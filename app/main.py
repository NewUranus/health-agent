"""FastAPI 应用、SSE 会话与可导入外部平台的工具接口。"""
import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import UploadFile
from app.agent import HealthAgent
from app import clock
from app.followup import plan_status
from app.memory import load_followup, load_memory
from app.ingest import IngestError, chunk_pages, parse_pdf
from app.knowledge import KnowledgeError

ROOT = Path(__file__).resolve().parent.parent


class MemoryPDFParser(MultiPartParser):
    # 请求总量在流读取时限制；阈值以内的 PDF 始终留在内存中。
    spool_max_size = 21 * 1024 * 1024


class ChunkUpdate(BaseModel):
    text: str


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=12000)


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RiskRequest(ToolRequest):
    sex: Literal["male", "female"]
    age: int = Field(ge=30, le=74)
    total_chol_mmol: float = Field(gt=0)
    hdl_mmol: float = Field(gt=0)
    sbp: int = Field(gt=0)
    bp_treated: bool
    smoker: bool
    diabetes: bool


class BMIRequest(ToolRequest):
    height_cm: float = Field(gt=0)
    weight_kg: float = Field(gt=0)


class BPRequest(ToolRequest):
    sbp: int = Field(gt=0)
    dbp: int = Field(ge=0)


class SimulationRequest(RiskRequest):
    quit_smoking: bool = False
    sbp_target: int | None = Field(default=None, gt=0)
    total_chol_target: float | None = Field(default=None, gt=0)


class TrajectoryRequest(RiskRequest):
    years: list[int] | None = None


def create_app():
    load_dotenv(ROOT / ".env")
    mock = os.getenv("HEALTH_AGENT_MOCK") == "1"

    @asynccontextmanager
    async def lifespan(application):
        async with HealthAgent(mock=mock) as agent:
            application.state.agent = agent
            yield

    application = FastAPI(title="HealthPilot 健康工具", description="心血管健康评估技术演示，不构成医疗建议。", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @application.middleware("http")
    async def admin_auth(request: Request, call_next):
        if request.url.path.startswith("/api/admin/"):
            # 没配置 ADMIN_PASSWORD 时后台免登录，配置后才校验
            password = os.getenv("ADMIN_PASSWORD")
            authorization = request.headers.get("authorization", "")
            if password and not hmac.compare_digest(authorization.encode(), ("Bearer " + password).encode()):
                return JSONResponse(status_code=401, content={"detail": "密码错误"})
        return await call_next(request)

    @application.exception_handler(KnowledgeError)
    async def knowledge_error(request: Request, exc):
        status = 404 if str(exc) in ("文档不存在。", "片段不存在。") else 409
        if status != 404 and request.url.path.endswith("/publish"):
            status = 502
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        status = 400 if request.url.path.startswith("/api/tools/") else 422
        return JSONResponse(status_code=status, content={"detail": "参数格式或范围无效，请按照接口说明填写。"})

    @application.get("/admin", include_in_schema=False)
    @application.get("/", include_in_schema=False)
    async def index():
        # 首页每次都向服务器确认版本；assets 的文件名带哈希，可以放心缓存
        return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    @application.get("/api/health")
    async def health():
        return {"ok": True, "mock": mock}

    @application.post("/api/admin/documents", status_code=201)
    async def upload_document(request: Request):
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise HTTPException(400, "请使用文件上传表单。")

        async def limited_stream():
            size = 0
            async for part in request.stream():
                size += len(part)
                if size > MemoryPDFParser.spool_max_size:
                    raise HTTPException(413, "文件超过 20MB，请拆分后上传。")
                yield part

        parser = MemoryPDFParser(request.headers, limited_stream(), max_files=1, max_fields=3)
        try:
            form = await parser.parse()
        except MultiPartException:
            raise HTTPException(400, "上传表单格式无效。") from None
        except BaseException:
            for temporary in parser._files_to_close_on_error:
                temporary.close()
            raise
        try:
            file, title = form.get("file"), form.get("title")
            if not isinstance(file, UploadFile) or not isinstance(title, str) or not title.strip():
                raise HTTPException(400, "请提供 PDF 文件和标题。")
            source, version = form.get("source", ""), form.get("version", "")
            if not isinstance(source, str) or not isinstance(version, str):
                raise HTTPException(400, "来源和版本必须为文本。")
            data = await file.read(20 * 1024 * 1024 + 1)
            if len(data) > 20 * 1024 * 1024:
                raise HTTPException(413, "文件超过 20MB，请拆分后上传。")
            try:
                pages = await asyncio.to_thread(parse_pdf, data)
                chunks = await asyncio.to_thread(chunk_pages, pages)
            except IngestError as exc:
                raise HTTPException(400, str(exc)) from None
            if not chunks:
                raise HTTPException(400, "没有可用的文字片段。")
            return await application.state.agent.kb.create_draft(title.strip(), source, version, chunks, file.filename or "")
        finally:
            await form.close()

    @application.get("/api/admin/documents")
    async def documents():
        return {"documents": await application.state.agent.kb.list_documents()}

    @application.get("/api/admin/documents/{doc_id}")
    async def document(doc_id: int):
        return await application.state.agent.kb.get_document(doc_id)

    @application.patch("/api/admin/documents/{doc_id}/chunks/{chunk_id}")
    async def update_chunk(doc_id: int, chunk_id: str, body: ChunkUpdate):
        if not body.text.strip():
            raise HTTPException(400, "片段文本不能为空。")
        return await application.state.agent.kb.update_chunk(doc_id, chunk_id, body.text)

    @application.delete("/api/admin/documents/{doc_id}/chunks/{chunk_id}")
    async def delete_chunk(doc_id: int, chunk_id: str):
        return await application.state.agent.kb.delete_chunk(doc_id, chunk_id)

    @application.post("/api/admin/documents/{doc_id}/publish")
    async def publish(doc_id: int):
        return await application.state.agent.kb.publish(doc_id)

    @application.delete("/api/admin/documents/{doc_id}")
    async def delete_document(doc_id: int):
        await application.state.agent.kb.delete_document(doc_id)
        return {"ok": True}

    @application.get("/api/admin/stats")
    async def stats():
        return await application.state.agent.kb.stats()

    @application.get("/api/admin/chunks")
    async def chunks(document_id: int | None = None, q: str | None = None,
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return await application.state.agent.kb.list_chunks(document_id, q, limit, offset)

    @application.get("/api/admin/search")
    async def search(q: str, k: int = Query(4, ge=1, le=200)):
        kb = application.state.agent.kb
        hits = await kb.search(q, k)
        return {"results": [{key: getattr(chunk, key) for key in ("id", "title", "source", "page", "text", "score")} for chunk in hits],
                "min_similarity": kb.min_similarity}

    @application.post("/api/chat")
    async def chat(body: ChatRequest):
        async def events():
            async for event in application.state.agent.stream(body.thread_id, body.message, body.user_id):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @application.get("/api/history")
    async def history(user_id: str):
        _, records = await load_memory(application.state.agent.store, user_id)
        return {"records": records}

    @application.get("/api/followup")
    async def followup(user_id: str):
        store = application.state.agent.store
        status = plan_status(await load_followup(store, user_id), clock.today())
        _, history = await load_memory(store, user_id)
        if history:
            status["last_risk_percent"] = history[-1]["risk_percent"]
        return status

    async def forward(name, body):
        try:
            return await application.state.agent.mcp.call_tool(name, body.model_dump())
        except ValueError:
            raise HTTPException(status_code=400, detail="工具参数无效，请检查输入范围。") from None
        except Exception:
            raise HTTPException(status_code=503, detail="健康工具暂不可用，请稍后重试。") from None

    @application.post("/api/tools/cvd_risk", summary="计算十年心血管病风险")
    async def cvd_risk(body: RiskRequest):
        return await forward("cvd_risk", body)

    @application.post("/api/tools/bmi", summary="计算中国标准 BMI")
    async def bmi(body: BMIRequest):
        return await forward("bmi", body)

    @application.post("/api/tools/bp_category", summary="血压水平分级")
    async def bp_category(body: BPRequest):
        return await forward("bp_category", body)

    @application.post("/api/tools/simulate_intervention", summary="模拟干预后的风险")
    async def simulate_intervention(body: SimulationRequest):
        return await forward("simulate_intervention", body)

    @application.post("/api/tools/project_trajectory", summary="推演健康风险轨迹")
    async def project_trajectory(body: TrajectoryRequest):
        return await forward("project_trajectory", body)

    return application


app = create_app()
