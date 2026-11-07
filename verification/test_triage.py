"""急症双通道：规则优先、模型补漏、失败退回规则、依据必须出自原文、随访中途同样生效。"""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage

import app.graph as graph_module
from app.graph import Nodes
from app.model_gateway import ModelGateway
from app.safety import parse_triage, triage


class FakeLLM:
    def __init__(self, *contents, error=None, delay=0):
        self.contents, self.error, self.delay, self.calls, self.seen = list(contents), error, delay, 0, []

    async def ainvoke(self, messages):
        self.calls += 1
        self.seen.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return AIMessage(content=self.contents[min(self.calls, len(self.contents)) - 1])


def verdict(emergency, category="", evidence=""):
    return json.dumps({"emergency": emergency, "category": category, "evidence": evidence}, ensure_ascii=False)


def run(coro):
    return asyncio.run(coro)


def nodes(llm, mock=False):
    return Nodes(None, None, None, llm, mock)


def test_rule_hit_skips_model_and_model_cannot_undo_it():
    llm = FakeLLM(verdict(False))
    update = run(nodes(llm).safety_check({"text": "胸痛二十分钟还没缓解"}))
    assert llm.calls == 0
    assert update["red_flags"] == ["chest_pain"]
    assert update["event_data"] == {"red_flags": ["chest_pain"], "source": "rule", "classifier": "not_needed"}


def test_model_catches_what_rules_miss():
    text = "心口像被石头压住，喘气都费劲"
    llm = FakeLLM(verdict(True, "chest_pain", "心口像被石头压住"))
    update = run(nodes(llm).safety_check({"text": text}))
    assert llm.calls == 1
    assert update["red_flags"] == ["chest_pain"]
    data = update["event_data"]
    assert data["source"] == "model" and data["classifier"] == "ok" and data["evidence"] == "心口像被石头压住"
    assert "classifier_failed" not in data
    # 用户原文作为数据传给模型，不拼进系统提示
    assert text in llm.seen[0][1].content and text not in llm.seen[0][0].content


def test_model_negative_keeps_flow():
    update = run(nodes(FakeLLM(verdict(False))).safety_check({"text": "高血压吃什么好"}))
    assert update["red_flags"] == []
    assert update["event_data"] == {"red_flags": [], "source": "none", "classifier": "ok"}


@pytest.mark.parametrize("raw", [
    verdict(True, "chest_pain", "胸口像被大象踩着"),  # 依据不在原文
    verdict(True, "chest_pain", ""),
    "不是 JSON",
    json.dumps({"emergency": "yes"}),
    json.dumps(["emergency"]),
])
def test_invalid_model_output_falls_back_to_rules(raw):
    update = run(nodes(FakeLLM(raw)).safety_check({"text": "心口有点不对劲"}))
    assert update["red_flags"] == []
    assert update["event_data"]["classifier"] == "invalid" and update["event_data"]["classifier_failed"] is True


def test_model_failure_and_timeout_fall_back_to_rules(caplog):
    update = run(nodes(FakeLLM(error=PermissionError("secret-in-message"))).safety_check({"text": "心口有点不对劲"}))
    assert update["red_flags"] == []
    assert update["event_data"]["classifier"] == "failed" and update["event_data"]["classifier_failed"] is True
    assert "PermissionError" in caplog.text and "secret-in-message" not in caplog.text

    async def slow(_):
        await asyncio.sleep(1)

    result = run(triage("心口有点不对劲", slow, timeout=0.01))
    assert result.flags == () and result.classifier == "failed" and result.error_type == "TimeoutError"


def test_failure_still_honours_rules():
    llm = FakeLLM(error=RuntimeError("down"))
    update = run(nodes(llm).safety_check({"text": "妈妈刚才说话不清，嘴歪了"}))
    assert llm.calls == 0 and "stroke" in update["red_flags"]


def test_evidence_matching_and_categories():
    assert parse_triage("```json\n" + verdict(True, "stroke", "嘴角 往一边歪") + "\n```", "半边脸木木的，嘴角往一边歪") == ("stroke", "嘴角 往一边歪")
    assert parse_triage(verdict(True, "heart_attack", "人快晕了"), "心跳快，人快晕了") == ("other", "人快晕了")
    assert parse_triage(verdict(False), "随便说说") is None
    with pytest.raises(ValueError):
        parse_triage(verdict(True, "stroke", "x" * 201), "x" * 300)


def test_mock_and_missing_model_only_use_rules():
    llm = FakeLLM(verdict(True, "chest_pain", "心口"))
    for target in (nodes(llm, mock=True), nodes(None)):
        update = run(target.safety_check({"text": "心口像被石头压住"}))
        assert update["red_flags"] == [] and update["event_data"]["classifier"] == "skipped"
    assert llm.calls == 0


def test_successful_verdicts_are_reused_but_failures_retry():
    llm = FakeLLM(verdict(False))
    target = nodes(llm)
    run(target.triage("最近睡不好"))
    run(target.triage("最近睡不好"))
    assert llm.calls == 1

    failing = FakeLLM(error=RuntimeError("down"))
    target = nodes(failing)
    run(target.triage("最近睡不好"))
    run(target.triage("最近睡不好"))
    assert failing.calls == 2


def test_checkin_answer_triggers_emergency_through_model(monkeypatch):
    answers = iter(["130/85", "心口像被石头压住，冒冷汗"])
    monkeypatch.setattr(graph_module, "interrupt", lambda payload: next(answers))
    llm = FakeLLM(verdict(False), verdict(True, "chest_pain", "心口像被石头压住"))
    state = {"profile": {"sex": "male", "age": 55, "total_chol": 5.5, "hdl": 1.3, "sbp": 140, "dbp": 90,
                         "bp_treated": False, "smoker": False, "diabetes": False},
             "parsed": {}, "history": [{"risk_percent": 10.0}], "user_id": "u"}
    # mcp 为 None：急症中止后不能再调用风险工具或写入记忆
    update = run(nodes(llm).checkin(state))
    assert llm.calls == 2
    assert "120" in update["reply"]
    assert update["event_data"]["red_flags"] == ["chest_pain"] and update["event_data"]["source"] == "model"


def test_triage_is_a_valid_model_route(monkeypatch):
    monkeypatch.setenv("QWEN_MODEL_ROUTES", json.dumps({"triage": "small-model"}))
    assert ModelGateway(FakeLLM(), "main").routes == {"triage": "small-model"}
