"""端到端：Mock 模式下走 HTTP + SSE + LangGraph 多 Agent + MCP 子进程。"""

import json
import re
import uuid

import pytest

from conftest import ROOT, chat, node_data, nodes, reply

FULL = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"
FULL_SMOKER = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，抽烟，没有糖尿病，没吃降压药"


def uid() -> str:
    return uuid.uuid4().hex


def test_index_and_health(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert client.get("/api/health").json() == {"ok": True, "mock": True}


def test_stream_shape(client):
    events = chat(client, uid(), "你好")
    assert events[-1] == {"type": "done"}
    assert events[0]["type"] == "node" and events[0]["node"] == "safety_check"
    for e in events:
        if e["type"] == "node":
            assert isinstance(e["agent"], str) and e["agent"]
            assert isinstance(e["ms"], int) and e["ms"] >= 0
    reply(events)


def test_emergency_short_circuits(client):
    events = chat(client, uid(), "突然胸口剧痛，出冷汗，喘不上气")
    assert node_data(events, "safety_check")["red_flags"]
    assert nodes(events) == ["safety_check", "emergency"]
    assert "120" in reply(events)


@pytest.fixture(scope="module")
def conversation(client):
    """同一个 thread 的三轮对话：先问缺失信息，再完成评估，最后追问。"""
    tid = uid()
    t1 = chat(client, tid, "我是男性，55岁，想评估一下心血管风险")
    t2 = chat(client, tid, "血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药")
    t3 = chat(client, tid, "我该怎么减盐？")
    return tid, t1, t2, t3


def test_turn1_asks_for_missing(conversation):
    _, t1, _, _ = conversation
    assert nodes(t1) == ["safety_check", "intake", "supervisor", "ask_missing"]
    assert node_data(t1, "supervisor")["route"] == "intake_agent"
    intake = node_data(t1, "intake")
    assert intake["profile"] == {"sex": "male", "age": 55}
    assert intake["missing"] == ["sbp", "total_chol", "hdl", "smoker", "diabetes", "bp_treated"]
    text = reply(t1)
    for word in ("血压", "总胆固醇", "高密度脂蛋白", "吸烟", "糖尿病", "降压药"):
        assert word in text


def test_turn2_full_assessment(conversation):
    _, _, t2, _ = conversation
    ns = nodes(t2)
    assert ns[:4] == ["safety_check", "intake", "supervisor", "risk_assess"]
    assert ns[-1] == "followup"
    for n in ("retrieve", "advise", "verify"):
        assert n in ns
    assert node_data(t2, "supervisor")["route"] == "assessment_agent"

    # 第二轮没说年龄性别，必须靠 checkpointer 记住
    assert node_data(t2, "intake")["profile"]["age"] == 55

    calls = node_data(t2, "risk_assess")["tool_calls"]
    cvd = [c for c in calls if c["tool"] == "cvd_risk"]
    assert cvd and cvd[0]["via"] == "mcp"
    assert cvd[0]["result"]["risk_percent"] == pytest.approx(13.45, abs=0.01)
    assert {"bp_category", "project_trajectory"} <= {c["tool"] for c in calls}
    assert all(isinstance(c["cached"], bool) for c in calls)

    v = node_data(t2, "verify")
    assert v["ok"] is True and v["fallback"] is False
    assert node_data(t2, "advise")["prompt_version"]

    text = reply(t2)
    assert "## 评估结果" in text
    assert "10年心血管病风险：13.5%" in text
    assert "中风险" in text
    assert "血压分级：1级高血压" in text
    assert "## 风险轨迹" in text
    assert "5年后（60岁）：17.2%" in text
    assert "10年后（65岁）：21.4%" in text
    assert "## 干预建议" in text
    assert "## 随访计划" in text
    assert "3个月" in text.replace(" ", "")
    assert "不构成医疗建议" in text

    cited = set(re.findall(r"\[cite:([\w-]+)\]", text))
    retrieved = {c["id"] for c in node_data(t2, "retrieve")["chunks"]}
    assert cited and cited <= retrieved
    assert "salt_reduction" in retrieved  # 血压偏高 → 应检索限盐


def test_turn3_followup_question(conversation):
    _, _, _, t3 = conversation
    ns = nodes(t3)
    assert ns == ["safety_check", "intake", "supervisor", "answer"]
    assert node_data(t3, "supervisor")["route"] == "qa_agent"
    data = node_data(t3, "answer")
    assert data["chunks"][0]["id"] == "salt_reduction"
    assert data["tool_calls"] == []
    assert "[cite:salt_reduction]" in reply(t3)


def test_agent_ownership(conversation):
    _, _, t2, t3 = conversation
    owner = {e["node"]: e["agent"] for e in t2 + t3 if e["type"] == "node"}
    assert owner["safety_check"] == "orchestrator"
    assert owner["intake"] == "orchestrator"
    assert owner["supervisor"] == "orchestrator"
    assert owner["risk_assess"] == "assessment_agent"
    assert owner["retrieve"] == "intervention_agent"
    assert owner["advise"] == "intervention_agent"
    assert owner["verify"] == "intervention_agent"
    assert owner["followup"] == "followup_agent"
    assert owner["answer"] == "qa_agent"


def test_trace_written(conversation):
    tid, *_ = conversation
    path = ROOT / "runs" / "traces" / f"{tid}.jsonl"
    assert path.exists()
    assert any(json.loads(line).get("node") == "followup" for line in path.read_text().splitlines())


def test_threads_are_isolated(client):
    events = chat(client, uid(), "血压130/85")
    intake = node_data(events, "intake")
    assert intake["profile"] == {"sbp": 130, "dbp": 85}
    assert intake["from_memory"] == []


def test_simulation_sbp(client):
    tid = uid()
    chat(client, tid, FULL)
    events = chat(client, tid, "如果我把血压降到120，风险能降多少？")
    assert nodes(events)[-1] == "simulate"
    assert node_data(events, "supervisor")["route"] == "simulation_agent"
    call = node_data(events, "simulate")["tool_calls"][0]
    assert call["tool"] == "simulate_intervention" and call["via"] == "mcp"
    assert call["args"]["sbp_target"] == 120
    text = reply(events)
    assert "当前风险：13.5%" in text
    assert "模拟后风险：10.2%" in text
    assert "绝对下降 3.3 个百分点" in text


def test_simulation_does_not_change_profile(client):
    tid = uid()
    chat(client, tid, FULL_SMOKER)
    events = chat(client, tid, "如果戒烟，不吸烟的话风险能降多少？")
    assert node_data(events, "intake")["profile"]["smoker"] is True
    result = node_data(events, "simulate")["tool_calls"][0]["result"]
    assert result["baseline"] == pytest.approx(24.27, abs=0.01)
    assert result["simulated"] == pytest.approx(13.45, abs=0.01)


def test_long_term_memory_checkin_across_threads(client):
    user = uid()
    first = uid()
    _post(client, first, user, FULL)
    second = uid()  # 新会话，同一个用户
    events = _post(client, second, user, "我来复查，最近血压130/85")
    intake = node_data(events, "intake")
    assert {"age", "sex", "total_chol", "hdl"} <= set(intake["from_memory"])
    assert "sbp" not in intake["from_memory"]
    assert node_data(events, "supervisor")["route"] == "checkin_agent"
    data = node_data(events, "checkin")
    assert data["previous_risk"] == pytest.approx(13.45, abs=0.01)
    assert data["current_risk"] == pytest.approx(11.77, abs=0.01)
    assert data["delta"] == pytest.approx(-1.68, abs=0.01)
    assert data["tool_calls"][0]["via"] == "mcp"
    text = reply(events)
    assert "上次风险：13.5%" in text
    assert "本次风险：11.8%" in text

    # 第三个会话应该看到第二次的记录
    third = _post(client, uid(), user, "我来复查，最近血压120/80")
    assert node_data(third, "checkin")["previous_risk"] == pytest.approx(11.77, abs=0.01)


def test_memory_is_per_user(client):
    _post(client, uid(), uid(), FULL)
    events = _post(client, uid(), uid(), "我来复查，最近血压130/85")
    assert node_data(events, "intake")["from_memory"] == []
    assert node_data(events, "supervisor")["route"] == "intake_agent"


def _post(client, thread_id: str, user_id: str, message: str) -> list[dict]:
    events = []
    body = {"thread_id": thread_id, "user_id": user_id, "message": message}
    with client.stream("POST", "/api/chat", json=body) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


def test_rest_tools_for_dify(client):
    body = {
        "sex": "male",
        "age": 55,
        "total_chol_mmol": 5.5,
        "hdl_mmol": 1.3,
        "sbp": 140,
        "bp_treated": False,
        "smoker": False,
        "diabetes": False,
    }
    r = client.post("/api/tools/cvd_risk", json=body)
    assert r.status_code == 200
    assert r.json()["risk_percent"] == pytest.approx(13.45, abs=0.01)
    assert client.post("/api/tools/cvd_risk", json={**body, "age": 80}).status_code == 400
    paths = client.get("/openapi.json").json()["paths"]
    for name in ("cvd_risk", "bmi", "bp_category", "simulate_intervention", "project_trajectory"):
        assert f"/api/tools/{name}" in paths
