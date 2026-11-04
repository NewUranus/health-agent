"""FastAPI 应用、SSE 会话与可导入外部平台的工具接口。"""
import asyncio
import json
import os
import time
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.datastructures import UploadFile
from app.agent import HealthAgent
from app import clock
from app.followup import plan_status
from app.memory import load_followup, load_memory
from app.ingest import IngestError, chunk_pages, clean_pages, parse_pdf
from app.knowledge import KnowledgeError
from app.auth import AuthFailure, AuthStore, COOKIE_NAME
from app.approvals import ApprovalStore, attach_approval_api
from app.http_security import attach_security, auth_required, current_identity, error_response, ErrorInfo
from app.tasks import TaskRunner, TaskStore
from app.task_api import attach_task_api
from app.knowledge_archive import attach_knowledge_archive

ROOT = Path(__file__).resolve().parent.parent
API_VERSION = "v1"
API_PREFIX = "/api/v1"


def v1_resource_path(path: str) -> str | None:
    """将 V1 正式路径和兼容路径映射为相同资源，未知版本不映射。"""
    for prefix in (API_PREFIX, "/api"):
        if path.startswith(prefix + "/"):
            resource = path[len(prefix):]
            if resource.split("/", 2)[1] in {"health", "chat", "history", "followup", "tools", "admin", "auth", "tasks"}:
                return resource
    return None


class MemoryPDFParser(MultiPartParser):
    # 请求总量在流读取时限制；阈值以内的 PDF 始终留在内存中。
    spool_max_size = 21 * 1024 * 1024


class ChunkUpdate(BaseModel):
    text: str


class ChatRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=12000)
    web_search: bool = False


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
    required = auth_required(mock)
    task_timeout = int(os.getenv("HEALTH_AGENT_TASK_TIMEOUT", "300"))
    if not 1 <= task_timeout <= 3600:
        raise ValueError("HEALTH_AGENT_TASK_TIMEOUT 须为 1–3600 秒。")
    embedded_worker = os.getenv("HEALTH_AGENT_TASK_WORKER", "embedded")
    if embedded_worker not in {"embedded", "external"}:
        raise ValueError("HEALTH_AGENT_TASK_WORKER 只支持 embedded 或 external。")
    if required and embedded_worker == "external" and not os.getenv("DATABASE_URL"):
        raise ValueError("独立 Worker 模式需要 DATABASE_URL。")

    @asynccontextmanager
    async def lifespan(application):
        if required:
            application.state.auth_store = await asyncio.to_thread(AuthStore)
            await asyncio.to_thread(application.state.auth_store.setup)
            application.state.login_slots = asyncio.Semaphore(2)
            application.state.task_store = TaskStore(application.state.auth_store)
            await asyncio.to_thread(application.state.task_store.setup)
            application.state.approval_store = ApprovalStore(application.state.auth_store)
            await asyncio.to_thread(application.state.approval_store.setup)
        async with HealthAgent(mock=mock) as agent:
            application.state.agent = agent
            runner = TaskRunner(application.state.task_store, agent, timeout=task_timeout) if required and embedded_worker == "embedded" else None
            if runner:
                await runner.start()
            try:
                yield
            finally:
                if runner:
                    await runner.close()

    application = FastAPI(
        title="HealthPilot API V1", version="1.0.0",
        description="心血管健康评估技术演示，不构成医疗建议。正式入口为 /api/v1；/api 是固定指向 V1 的兼容入口。当前 Agent 版本为 v1。",
        lifespan=lifespan,
    )
    application.state.auth_required = required
    application.state.auth_store = None
    api_v1 = APIRouter(responses={status: {"model": ErrorInfo} for status in (400, 401, 403, 404, 409, 422, 428, 429, 500, 503)})
    application.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    attach_security(application, api_v1, v1_resource_path,
                    {"X-API-Version": API_VERSION, "X-Agent-Version": HealthAgent.version})
    attach_task_api(api_v1)
    attach_approval_api(api_v1)
    attach_knowledge_archive(api_v1)

    @application.exception_handler(KnowledgeError)
    async def knowledge_error(request: Request, exc):
        status = 404 if str(exc) in ("文档不存在。", "片段不存在。") else 409
        if status != 404 and request.url.path.endswith("/publish"):
            status = 502
        return error_response(request, status, str(exc))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        resource = v1_resource_path(request.url.path)
        status = 400 if resource and resource.startswith("/tools/") else 422
        return error_response(request, status, "参数格式或范围无效，请按照接口说明填写。")

    @application.get("/admin", include_in_schema=False)
    @application.get("/", include_in_schema=False)
    async def index():
        # 首页每次都向服务器确认版本；assets 的文件名带哈希，可以放心缓存
        return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    @api_v1.get("/health")
    async def health():
        return {"ok": True, "mock": mock}

    @api_v1.post("/admin/documents", status_code=201)
    @api_v1.post("/admin/document-pairs", status_code=201)
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
            if request.url.path.endswith('/document-pairs'):
                cleaned = await asyncio.to_thread(clean_pages, pages)
                clean_chunks = await asyncio.to_thread(chunk_pages, cleaned)
                if not clean_chunks:
                    raise HTTPException(400, "清洗后没有可用的文字片段。")
                documents = await application.state.agent.kb.create_comparison(
                    title.strip(), source, version, chunks, clean_chunks, file.filename or '')
                return {'documents': documents}
            return await application.state.agent.kb.create_draft(title.strip(), source, version, chunks, file.filename or "")
        finally:
            await form.close()

    @api_v1.get("/admin/documents")
    async def documents():
        return {"documents": await application.state.agent.kb.list_documents()}

    @api_v1.get("/admin/accounts", summary="查看可用于知识授权的账号标识")
    async def knowledge_accounts():
        if not required:
            return {"accounts": []}
        def read():
            with application.state.auth_store.connection() as execute:
                return [dict(row) for row in execute("SELECT id, username FROM hp_users ORDER BY username LIMIT 500").fetchall()]
        return {"accounts": await asyncio.to_thread(read)}

    @api_v1.get("/admin/model-usage", summary="查看最近七天模型调用用量，不含提示正文")
    async def model_usage():
        if not required:
            return {"groups": [], "currency_cost": None}
        def read():
            with application.state.auth_store.connection() as execute:
                return [dict(row) for row in execute("SELECT model, scene, status, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(duration_ms) AS duration_ms, SUM((SELECT cost_microusd FROM hp_model_costs c WHERE c.usage_id = hp_model_usage.id)) AS cost_microusd, SUM(CASE WHEN EXISTS (SELECT 1 FROM hp_model_costs c WHERE c.usage_id = hp_model_usage.id) THEN 1 ELSE 0 END) AS priced_calls, SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL THEN 1 ELSE 0 END) AS unknown_usage_calls FROM hp_model_usage WHERE at >= ? GROUP BY model, scene, status", (int(time.time()) - 7 * 86400,)).fetchall()]
        return {"groups": await asyncio.to_thread(read), "currency_cost": None}

    @api_v1.get("/admin/documents/{doc_id}")
    async def document(doc_id: int):
        return await application.state.agent.kb.get_document(doc_id)

    @api_v1.patch("/admin/documents/{doc_id}/chunks/{chunk_id}")
    async def update_chunk(doc_id: int, chunk_id: str, body: ChunkUpdate):
        if not body.text.strip():
            raise HTTPException(400, "片段文本不能为空。")
        return await application.state.agent.kb.update_chunk(doc_id, chunk_id, body.text)

    @api_v1.delete("/admin/documents/{doc_id}/chunks/{chunk_id}")
    async def delete_chunk(doc_id: int, chunk_id: str):
        return await application.state.agent.kb.delete_chunk(doc_id, chunk_id)

    @api_v1.post("/admin/documents/{doc_id}/publish")
    async def publish(request: Request, doc_id: int):
        if required:
            raise AuthFailure(428, "APPROVAL_REQUIRED", "发布会影响医学问答知识，请先核对确认单。")
        return await application.state.agent.kb.publish(doc_id)

    @api_v1.delete("/admin/documents/{doc_id}")
    async def delete_document(request: Request, doc_id: int):
        if required:
            raise AuthFailure(428, "APPROVAL_REQUIRED", "删除无法撤销，请先核对确认单。")
        await application.state.agent.kb.delete_document(doc_id)
        return {"ok": True}

    @api_v1.get("/admin/stats")
    async def stats():
        return await application.state.agent.kb.stats()

    @api_v1.get("/admin/chunks")
    async def chunks(document_id: int | None = None, q: str | None = None,
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return await application.state.agent.kb.list_chunks(document_id, q, limit, offset)

    @api_v1.get("/admin/search")
    async def search(q: str, k: int = Query(4, ge=1, le=200), document_id: int | None = Query(None, ge=1)):
        kb = application.state.agent.kb
        hits = await kb.search(q, k, document_id=document_id)
        return {"results": [{key: getattr(chunk, key) for key in ("id", "title", "source", "page", "text", "score")} for chunk in hits],
                "min_similarity": kb.min_similarity}

    @api_v1.get("/admin/compare")
    async def compare(q: str = Query(min_length=1), document_id: int = Query(ge=1),
                      k: int = Query(4, ge=1, le=200)):
        return await application.state.agent.kb.compare(document_id, q, k)

    @api_v1.post("/chat")
    async def chat(request: Request, body: ChatRequest):
        user_id = current_identity(request, body.user_id, body.thread_id)
        thread_id = body.thread_id
        if required:
            thread_id = await asyncio.to_thread(application.state.auth_store.claim_thread, thread_id, request.state.user["id"])
        async def events():
            options = {"web_search": True} if body.web_search else {}
            outcome = "chat.interrupted"
            try:
                async with aclosing(application.state.agent.stream(thread_id, body.message, user_id, **options)) as stream:
                    async for event in stream:
                        if required and not await asyncio.to_thread(application.state.auth_store.resolve, request.cookies.get(COOKIE_NAME)):
                            yield "data: " + json.dumps({"type": "error", "message": "登录已失效，请重新登录。", "code": "AUTH_REQUIRED", "request_id": request.state.request_id}, ensure_ascii=False) + "\n\n"
                            yield 'data: {"type":"done"}\n\n'
                            break
                        if event["type"] == "error":
                            outcome = "chat.failed"
                            event = {**event, "code": "AGENT_EXECUTION_FAILED", "request_id": request.state.request_id,
                                     "retryable": False, "next_action": "检查输入；重新执行前请先核对随访记录。"}
                        if event["type"] == "done" and outcome != "chat.failed":
                            outcome = "chat.completed"
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                if required:
                    await asyncio.to_thread(application.state.auth_store.audit, request.state.user["id"], outcome,
                                            thread_id, request.state.request_id, 200 if outcome == "chat.completed" else 409)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api_v1.get("/history")
    async def history(request: Request, user_id: str | None = None):
        user_id = current_identity(request, user_id)
        _, records = await load_memory(application.state.agent.store, user_id)
        return {"records": records}

    @api_v1.get("/followup")
    async def followup(request: Request, user_id: str | None = None):
        user_id = current_identity(request, user_id)
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

    @api_v1.post("/tools/cvd_risk", summary="计算十年心血管病风险")
    async def cvd_risk(body: RiskRequest):
        return await forward("cvd_risk", body)

    @api_v1.post("/tools/bmi", summary="计算中国标准 BMI")
    async def bmi(body: BMIRequest):
        return await forward("bmi", body)

    @api_v1.post("/tools/bp_category", summary="血压水平分级")
    async def bp_category(body: BPRequest):
        return await forward("bp_category", body)

    @api_v1.post("/tools/simulate_intervention", summary="模拟干预后的风险")
    async def simulate_intervention(body: SimulationRequest):
        return await forward("simulate_intervention", body)

    @api_v1.post("/tools/project_trajectory", summary="推演健康风险轨迹")
    async def project_trajectory(body: TrajectoryRequest):
        return await forward("project_trajectory", body)

    # 两个入口注册同一套处理函数，不重定向 POST 或 SSE，也不维护重复逻辑。
    application.include_router(api_v1, prefix=API_PREFIX, tags=["V1"])
    application.include_router(api_v1, prefix="/api", tags=["V1 兼容入口"])
    return application


app = create_app()
