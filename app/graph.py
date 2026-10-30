"""确定性主图路由与问诊、评估、干预、随访、模拟、复查、问答子图。"""
import asyncio
import json
import logging
import re
from dataclasses import asdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from app.function_calling import mcp_tools_to_openai, run_tool_loop
from app.intake import FIELD_LABELS, missing_fields, parse_profile
from app.llm import template_advice
from app.memory import load_memory, save_assessment
from app.prompts import load_prompt
from app.safety import check_red_flags
from app.verify import verify_advice

logger = logging.getLogger(__name__)

DISCLAIMER = "> 仅为技术演示，不构成医疗建议。"
OWNERS = {"safety_check": "orchestrator", "emergency": "orchestrator", "intake": "orchestrator", "supervisor": "orchestrator",
          "ask_missing": "intake_agent", "risk_assess": "assessment_agent", "retrieve": "intervention_agent", "advise": "intervention_agent",
          "verify": "intervention_agent", "followup": "followup_agent", "simulate": "simulation_agent", "checkin": "checkin_agent", "answer": "qa_agent"}


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    user_id: str
    text: str
    profile: dict
    parsed: dict
    history: list
    red_flags: list
    missing: list
    assessment: dict
    retrieved: list
    advice: str
    verify: dict
    attempt: int
    followup: str
    stage: str
    reply: str
    route: str
    unsupported: bool
    event_data: dict


def display_number(value):
    """显示采用十进制四舍五入，避免 13.45 被浮点格式化为 13.4。"""
    return str(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def risk_args(profile):
    return {"sex": profile["sex"], "age": profile["age"], "total_chol_mmol": profile["total_chol"], "hdl_mmol": profile["hdl"],
            **{key: profile[key] for key in ("sbp", "bp_treated", "smoker", "diabetes")}}


def result(data, **state):
    return {**state, "event_data": data}


def mark_degraded(data, node, exc):
    """记录降级原因；只记异常类型，异常文本可能带上请求内容。"""
    logger.warning("%s 节点模型调用失败，已降级：%s", node, type(exc).__name__)
    data.update(degraded=True, degraded_reason=type(exc).__name__)


class Nodes:
    def __init__(self, mcp, store, retriever, llm, mock):
        self.mcp, self.store, self.retriever, self.llm, self.mock = mcp, store, retriever, llm, mock

    async def invoke(self, prompt, payload):
        if self.llm is None:
            raise RuntimeError("未配置模型")
        response = await self.llm.ainvoke([SystemMessage(prompt), HumanMessage(json.dumps(payload, ensure_ascii=False))])
        if not isinstance(response.content, str) or not response.content.strip():
            raise ValueError("模型未返回文本")
        return response.content

    async def safety_check(self, state):
        flags = check_red_flags(state["text"])
        return result({"red_flags": flags}, red_flags=flags)

    async def emergency(self, state):
        text = "这些描述可能涉及急症。请立即拨打120，尽快就医，不要自行驾车；请身边的人陪同并协助联系急救。"
        if "self_harm" in state["red_flags"]:
            text = "听起来你正承受很大的痛苦。请立即联系身边可信任的人陪伴，远离可能伤害自己的物品，并拨打120寻求紧急帮助。"
        return result({}, reply=text + "\n\n" + DISCLAIMER)

    async def intake(self, state):
        memory, history = await load_memory(self.store, state["user_id"])
        session = state.get("profile", {})
        parsed = parse_profile(state["text"])
        data = {}
        # 规则解析已凑齐必填字段就不调模型，问答和随访轮能快几秒
        if not self.mock and missing_fields({**memory, **session, **parsed}):
            try:
                raw = await self.invoke(load_prompt("extract").text, {"消息": state["text"]})
                extracted = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
                valid = {}
                for key, value in extracted.items():
                    if key == "sex" and value in ("male", "female"):
                        valid[key] = value
                    elif key in ("smoker", "diabetes", "bp_treated") and isinstance(value, bool):
                        valid[key] = value
                    elif key in ("age", "sbp", "dbp", "height_cm", "weight_kg", "total_chol", "hdl") and type(value) in (int, float) and 0 < value < 1000:
                        if key not in ("age", "sbp", "dbp") or value == int(value):
                            valid[key] = value
                parsed = {**valid, **parsed}
            except Exception as exc:
                mark_degraded(data, "intake", exc)
        if re.search(r"如果|假如|要是", state["text"]):
            parsed = {}
        profile = {**memory, **session, **parsed}
        missing = missing_fields(profile)
        data.update(profile=profile, missing=missing, from_memory=sorted(set(memory) - set(session) - set(parsed)))
        return result(data, profile=profile, parsed=parsed, history=history, missing=missing, attempt=0)

    async def supervisor(self, state):
        text, history = state["text"], state["history"]
        unsupported = False
        if state["missing"]:
            route, reason = "intake_agent", "信息不全"
        elif not 30 <= state["profile"]["age"] <= 74:
            # 风险模型算不了，但仍然可以回答指南类问题
            unsupported = bool(state["parsed"])
            route, reason = ("intake_agent", "年龄超出风险模型适用范围") if unsupported else ("qa_agent", "回答健康问题")
        elif history and re.search(r"如果|假如|要是|能降|降到", text):
            route, reason = "simulation_agent", "已有评估，模拟干预效果"
        elif history and re.search(r"随访|复查|复诊|最近血压|最新血压", text):
            route, reason = "checkin_agent", "已有评估，比较复查数据"
        elif state.get("stage") != "assessed" and not history:
            route, reason = "assessment_agent", "资料完整，进行首次评估"
        elif state.get("stage") != "assessed" and history and state["parsed"]:
            route, reason = "assessment_agent", "新会话提供了健康信息，重新评估"
        else:
            route, reason = "qa_agent", "回答健康问题"
        return result({"route": route, "reason": reason}, route=route, unsupported=unsupported)

    async def ask_missing(self, state):
        if state.get("unsupported"):
            reply = f"当前的风险模型只适用于 30–74 岁，{state['profile']['age']} 岁无法计算 10 年风险，建议咨询医生做个体化评估。您仍然可以继续问血压、饮食、运动等健康问题。\n\n{DISCLAIMER}"
            return result({"unsupported": True}, reply=reply)
        labels = [FIELD_LABELS[key] for key in state["missing"]]
        reply = "为了完成评估，请补充：" + "、".join(labels) + "。血脂单位为 mmol/L；未知信息请说明，不要猜测。"
        data = {"missing": state["missing"]}
        if not self.mock:
            try:
                generated = await self.invoke("用中文温和地追问所有缺失项，不推测数据，不诊断。只输出一句话。", {"缺失项": labels})
                reply = generated + "\n\n待补充：" + "、".join(labels) + "。"
            except Exception as exc:
                mark_degraded(data, "ask_missing", exc)
        return result(data, reply=reply)

    async def risk_assess(self, state):
        profile = state["profile"]
        args = risk_args(profile)
        calls = [("cvd_risk", args), ("bp_category", {"sbp": profile["sbp"], "dbp": profile.get("dbp", 0)}), ("project_trajectory", args)]
        if "height_cm" in profile and "weight_kg" in profile:
            calls.append(("bmi", {key: profile[key] for key in ("height_cm", "weight_kg")}))
        trace = await asyncio.gather(*(self.mcp.call_with_trace(name, values) for name, values in calls))
        return result({"tool_calls": trace}, assessment={call["tool"]: call["result"] for call in trace})

    async def retrieve(self, state):
        profile = state["profile"]
        queries = []
        if profile["sbp"] >= 120:
            queries.extend(["减盐 限盐", "降压目标"])
        if profile["smoker"]:
            queries.append("戒烟")
        if state["assessment"].get("bmi", {}).get("bmi", 0) >= 24:
            queries.append("减重 体重管理")
        queries.extend(["运动 每周多少分钟", "膳食"])
        if profile["total_chol"] >= 5.2:
            queries.append("总胆固醇 偏高")
        chunks = {}
        for query in queries:
            for chunk in await asyncio.to_thread(self.retriever.search, query, 1):
                chunks.setdefault(chunk.id, asdict(chunk))
        retrieved = list(chunks.values())[:6]
        return result({"chunks": retrieved}, retrieved=retrieved)

    async def advise(self, state):
        prompt = load_prompt("advise")
        advice = template_advice(state["retrieved"])
        data = {"prompt_version": prompt.version}
        if not self.mock:
            try:
                advice = await self.invoke(prompt.text, {"资料": state["retrieved"], "风险": state["assessment"], "校验问题": state.get("verify", {}).get("problems", [])})
            except Exception as exc:
                mark_degraded(data, "advise", exc)
        return result(data, advice=advice, attempt=state.get("attempt", 0) + 1)

    async def verify(self, state):
        allowed = [c["id"] for c in state["retrieved"]]
        verified = verify_advice(state["advice"], allowed)
        fallback = not verified.ok and state["attempt"] >= 3
        advice = state["advice"]
        problems = verified.problems
        if fallback:
            advice = template_advice(state["retrieved"])
            verified = verify_advice(advice, allowed)
        data = {"ok": verified.ok, "problems": verified.problems, "attempt": state["attempt"], "fallback": fallback}
        if fallback:
            data["original_problems"] = problems
        return result(data, verify=data, advice=advice)

    async def followup(self, state):
        assessment = state["assessment"]
        risk = assessment["cvd_risk"]
        followup = {"high": "建议1个月内就诊复查，由医生评估干预方案。", "intermediate": "建议3个月复查血压血脂。", "low": "建议12个月常规体检，并保持健康生活方式。"}[risk["level"]]
        await save_assessment(self.store, state["user_id"], state["profile"], risk)
        level = {"low": "低", "intermediate": "中", "high": "高"}[risk["level"]]
        lines = ["## 评估结果", f"10年心血管病风险：{display_number(risk['risk_percent'])}%（{level}风险）", f"血压分级：{assessment['bp_category']['category']}"]
        if "bmi" in assessment:
            lines.append(f"BMI：{assessment['bmi']['bmi']}（{assessment['bmi']['category']}）")
        points = [point for point in assessment["project_trajectory"]["points"] if point["years"] in (5, 10)]
        if points:
            lines += ["## 风险轨迹", *[f"{p['years']}年后（{p['age']}岁）：{display_number(p['risk_percent'])}%" for p in points], assessment["project_trajectory"]["note"]]
        lines += ["## 干预建议", state["advice"], "## 随访计划", followup, risk["note"], DISCLAIMER]
        return result({"followup": followup}, followup=followup, stage="assessed", reply="\n".join(lines))

    async def simulate(self, state):
        text = state["text"]
        changes = {}
        sbp = re.search(r"血压降到\s*(\d+)", text)
        chol = re.search(r"胆固醇降到\s*(\d+(?:\.\d+)?)", text)
        if sbp:
            changes["sbp_target"] = int(sbp[1])
        if chol:
            changes["total_chol_target"] = float(chol[1])
        if re.search(r"戒烟|不抽烟|不吸烟", text):
            changes["quit_smoking"] = True
        if not changes:
            changes["sbp_target"] = state["profile"]["sbp"] - 10
        trace = await self.mcp.call_with_trace("simulate_intervention", {**risk_args(state["profile"]), **changes})
        data = trace["result"]
        reply = f"## 干预模拟\n当前风险：{display_number(data['baseline'])}%\n模拟后风险：{display_number(data['simulated'])}%（绝对下降 {display_number(data['absolute_reduction'])} 个百分点）\n" + "模拟改变：" + "、".join(data["changes"]) + "\n" + data["note"] + "\n\n" + DISCLAIMER
        return result({"tool_calls": [trace]}, reply=reply)

    async def checkin(self, state):
        trace = await self.mcp.call_with_trace("cvd_risk", risk_args(state["profile"]))
        risk = trace["result"]
        previous = state["history"][-1]["risk_percent"]
        current = risk["risk_percent"]
        delta = round(current - previous, 2)
        await save_assessment(self.store, state["user_id"], state["profile"], risk)
        encouragement = "风险有所下降，请继续保持健康习惯。" if delta < 0 else "风险基本不变，请继续监测、坚持健康习惯。" if delta == 0 else "风险有所上升，建议及时就医，和医生讨论复查与干预方案。"
        if risk["level"] == "high":
            encouragement += "目前仍属高风险，建议1个月内就诊，由医生评估干预方案。"
        reply = f"## 随访对比\n上次风险：{display_number(previous)}%\n本次风险：{display_number(current)}%\n{encouragement}\n{risk['note']}\n\n{DISCLAIMER}"
        return result({"previous_risk": previous, "current_risk": current, "delta": delta, "tool_calls": [trace]}, reply=reply, stage="assessed")

    async def answer(self, state):
        chunks = [asdict(c) for c in await asyncio.to_thread(self.retriever.search, state["text"], 4)]
        prompt = load_prompt("answer")
        reply = template_advice(chunks)
        traces = []
        data = {"chunks": chunks, "tool_calls": traces, "prompt_version": prompt.version}
        if not self.mock:
            try:
                if self.llm is None:
                    raise RuntimeError("未配置模型")
                async def call_tool(name, args):
                    trace = await self.mcp.call_with_trace(name, args)
                    traces.append(trace)
                    return trace["result"]
                tools = mcp_tools_to_openai(await self.mcp.list_tools())
                final, loop_trace = await run_tool_loop(self.llm, tools, call_tool,
                    [SystemMessage(prompt.text), *state.get("messages", [])[-10:], HumanMessage(json.dumps({"问题": state["text"], "档案": state["profile"], "指南": chunks}, ensure_ascii=False))])
                for call in loop_trace:
                    if "error" in call["result"]:
                        traces.append({**call, "via": "mcp", "cached": False})
                content = final.content.replace(DISCLAIMER, "").strip()
                checked = verify_advice(content, [c["id"] for c in chunks], per_item=False)
                if checked.ok:
                    reply = content
                else:
                    data.update(fallback=True, problems=checked.problems)
            except Exception as exc:
                mark_degraded(data, "answer", exc)
        return result(data, retrieved=chunks, reply=reply + "\n\n" + DISCLAIMER)


def build_graph(mcp, store, retriever, llm, mock, checkpointer):
    nodes = Nodes(mcp, store, retriever, llm, mock)

    def wrapped(name):
        async def execute(state):
            update = await getattr(nodes, name)(state)
            if "reply" in update:
                update["messages"] = [AIMessage(update["reply"])]
            return update
        return execute

    def subgraph(names):
        graph = StateGraph(AgentState)
        for name in names:
            graph.add_node(name, wrapped(name))
        graph.add_edge(START, names[0])
        for before, after in zip(names, names[1:]):
            graph.add_edge(before, after)
        if names[-1] == "verify":
            graph.add_conditional_edges("verify", lambda s: END if s["verify"]["ok"] or s["verify"]["fallback"] else "advise", {END: END, "advise": "advise"})
        else:
            graph.add_edge(names[-1], END)
        return graph.compile()

    graph = StateGraph(AgentState)
    for name in ("safety_check", "emergency", "intake", "supervisor"):
        graph.add_node(name, wrapped(name))
    for name, leaves in {"intake_agent": ["ask_missing"], "assessment_agent": ["risk_assess"], "intervention_agent": ["retrieve", "advise", "verify"],
                         "followup_agent": ["followup"], "simulation_agent": ["simulate"], "checkin_agent": ["checkin"], "qa_agent": ["answer"]}.items():
        graph.add_node(name, subgraph(leaves))
    graph.add_edge(START, "safety_check")
    graph.add_conditional_edges("safety_check", lambda s: "emergency" if s["red_flags"] else "intake", {"emergency": "emergency", "intake": "intake"})
    graph.add_edge("emergency", END)
    graph.add_edge("intake", "supervisor")
    routes = ["intake_agent", "assessment_agent", "simulation_agent", "checkin_agent", "qa_agent"]
    graph.add_conditional_edges("supervisor", lambda s: s["route"], {r: r for r in routes})
    graph.add_edge("assessment_agent", "intervention_agent")
    graph.add_edge("intervention_agent", "followup_agent")
    for name in ("intake_agent", "followup_agent", "simulation_agent", "checkin_agent", "qa_agent"):
        graph.add_edge(name, END)
    return graph.compile(checkpointer=checkpointer, store=store)
