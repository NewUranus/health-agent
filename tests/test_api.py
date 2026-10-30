"""端到端：Mock 模式下走 HTTP + SSE + LangGraph + MCP 子进程。"""

import re
import uuid

import pytest

from conftest import chat, node_data, nodes, reply


def test_index_and_health(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert client.get("/api/health").json() == {"ok": True, "mock": True}


def test_stream_shape(client):
    events = chat(client, uuid.uuid4().hex, "你好")
    assert events[-1] == {"type": "done"}
    assert events[0] == {"type": "node", "node": "safety_check", "data": events[0]["data"]}
    reply(events)


def test_emergency_short_circuits(client):
    events = chat(client, uuid.uuid4().hex, "突然胸口剧痛，出冷汗，喘不上气")
    assert node_data(events, "safety_check")["red_flags"]
    assert "emergency" in nodes(events)
    for n in ("intake", "risk_assess", "advise", "answer"):
        assert n not in nodes(events)
    assert "120" in reply(events)


@pytest.fixture(scope="module")
def conversation(client):
    """同一个 thread 的三轮对话：先问缺失信息，再完成评估，最后追问。"""
    tid = uuid.uuid4().hex
    t1 = chat(client, tid, "我是男性，55岁，想评估一下心血管风险")
    t2 = chat(client, tid, "血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药")
    t3 = chat(client, tid, "我该怎么减盐？")
    return t1, t2, t3


def test_turn1_asks_for_missing(conversation):
    t1, _, _ = conversation
    assert nodes(t1) == ["safety_check", "intake", "ask_missing"]
    intake = node_data(t1, "intake")
    assert intake["profile"] == {"sex": "male", "age": 55}
    assert intake["missing"] == ["sbp", "total_chol", "hdl", "smoker", "diabetes", "bp_treated"]
    text = reply(t1)
    for word in ("血压", "总胆固醇", "高密度脂蛋白", "吸烟", "糖尿病", "降压药"):
        assert word in text


def test_turn2_full_assessment(conversation):
    _, t2, _ = conversation
    ns = nodes(t2)
    assert ns[:2] == ["safety_check", "intake"]
    for n in ("risk_assess", "retrieve", "advise", "verify", "followup"):
        assert n in ns
    assert ns[-1] == "followup"

    # 第二轮没说年龄性别，必须靠 checkpointer 记住
    assert node_data(t2, "intake")["profile"]["age"] == 55

    calls = node_data(t2, "risk_assess")["tool_calls"]
    cvd = [c for c in calls if c["tool"] == "cvd_risk"]
    assert cvd and cvd[0]["via"] == "mcp"
    assert cvd[0]["result"]["risk_percent"] == pytest.approx(13.45, abs=0.01)
    assert any(c["tool"] == "bp_category" for c in calls)

    v = node_data(t2, "verify")
    assert v["ok"] is True and v["fallback"] is False

    text = reply(t2)
    assert "## 评估结果" in text
    assert "10年心血管病风险：13.5%" in text
    assert "中风险" in text
    assert "血压分级：1级高血压" in text
    assert "## 干预建议" in text
    assert "## 随访计划" in text
    assert "3个月" in text.replace(" ", "")
    assert "不构成医疗建议" in text

    cited = set(re.findall(r"\[cite:([\w-]+)\]", text))
    retrieved = {c["id"] for c in node_data(t2, "retrieve")["chunks"]}
    assert cited and cited <= retrieved
    assert "salt_reduction" in retrieved  # 血压偏高 → 应检索限盐


def test_turn3_followup_question(conversation):
    _, _, t3 = conversation
    ns = nodes(t3)
    assert "answer" in ns
    assert "intake" not in ns and "risk_assess" not in ns
    ids = [c["id"] for c in node_data(t3, "answer")["chunks"]]
    assert ids[0] == "salt_reduction"
    assert "[cite:salt_reduction]" in reply(t3)


def test_threads_are_isolated(client):
    events = chat(client, uuid.uuid4().hex, "血压130/85")
    assert node_data(events, "intake")["profile"] == {"sbp": 130, "dbp": 85}
