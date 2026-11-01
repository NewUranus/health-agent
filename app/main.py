"""FastAPI 应用、SSE 会话与可导入外部平台的工具接口。"""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from app.agent import HealthAgent
from app import clock
from app.followup import plan_status
from app.memory import load_followup, load_memory

ROOT = Path(__file__).resolve().parent.parent


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

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc):
        status = 400 if request.url.path.startswith("/api/tools/") else 422
        return JSONResponse(status_code=status, content={"detail": "参数格式或范围无效，请按照接口说明填写。"})

    @application.get("/", include_in_schema=False)
    async def index():
        # 首页每次都向服务器确认版本；assets 的文件名带哈希，可以放心缓存
        return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})

    @application.get("/api/health")
    async def health():
        return {"ok": True, "mock": mock}

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
