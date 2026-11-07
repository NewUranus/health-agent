"""事实支持校验：拆句、说明句识别、判定解析、重写、删句、删光拒答、判定失败与跳过。"""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore

from app.graph import NO_EVIDENCE, Nodes
from app.grounding import check_grounding, parse_verdicts, remove_claims, split_claims
from app.llm import template_advice
from app.model_gateway import ModelGateway
from app.rag import Chunk

SOURCE = "一般高血压患者应将血压降至<140/90 mmHg。患者不应自行开始、停用或调整降压药。"


class Scripted:
    def __init__(self, *replies, error=None):
        self.replies, self.error, self.seen = list(replies), error, []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        if self.error:
            raise self.error
        return AIMessage(content=self.replies.pop(0))


class FakeKB:
    async def search(self, query, k=4, min_similarity=None):
        return [Chunk("bp_targets", "降压目标", "《指南》", SOURCE, 0.6)]


class FakeMCP:
    async def list_tools(self):
        return []


def verdicts(*supported):
    return json.dumps({"results": [{"id": index, "verdict": "supported" if ok else "unsupported", "reason": "测试"}
                                   for index, ok in enumerate(supported, 1)]}, ensure_ascii=False)


def payloads(judge):
    return [json.loads(messages[-1].content) for messages in judge.seen]


def answer(llm, judge, text="高血压要降到多少"):
    nodes = Nodes(FakeMCP(), InMemoryStore(), FakeKB(), llm, False, judge=judge)
    return asyncio.run(nodes.answer({"text": text, "profile": {}, "messages": []}))


GOOD = "- 一般患者血压应降到140/90 mmHg以下 [cite:bp_targets]\n- 不要自行停用降压药 [cite:bp_targets]"
BAD = GOOD + "\n- 部分常用降压药在孕期禁用 [cite:bp_targets]"


# ---------- 拆句与删句 ----------

def test_split_marks_meta_and_inherits_citations():
    text = ("## 建议\n不饮酒最好；成年人一天酒精不超过15克 [cite:alcohol]。\n"
            "具体做法如下：\n- 少吃咸菜。[cite:salt] 每天盐不超过5克 [cite:salt]\n"
            "资料未覆盖孕妇的情况，建议咨询医生。\n> 仅为技术演示，不构成医疗建议。")
    claims = split_claims(text)
    facts = [(claim.text, claim.cites) for claim in claims if not claim.meta]
    assert facts == [("不饮酒最好；", ("alcohol",)), ("成年人一天酒精不超过15克。", ("alcohol",)),
                     ("少吃咸菜。", ("salt",)), ("每天盐不超过5克", ("salt",))]
    assert {claim.text for claim in claims if claim.meta} >= {"## 建议", "具体做法如下：", "资料未覆盖孕妇的情况，建议咨询医生。"}


def test_remove_keeps_other_sentences_and_bullets():
    claims = split_claims(BAD)
    assert remove_claims(BAD, {claims[-1].index}) == GOOD
    text = "- 第一句。[cite:a] 第二句 [cite:a]\n- 第三句 [cite:a]"
    assert remove_claims(text, {1}) == "- 第二句 [cite:a]\n- 第三句 [cite:a]"
    assert remove_claims(text, {1, 2}) == "- 第三句 [cite:a]"


def test_verdict_parsing_survives_broken_reasons():
    assert parse_verdicts("```json\n" + verdicts(True, False) + "\n```") == {1: ("supported", "测试"), 2: ("unsupported", "测试")}
    broken = '{"results": [{"id": 1, "verdict": "unsupported", "reason": "资料没有"孕期"内容"}, {"id": 2, "verdict": "supported", "reason": "一致"}]}'
    assert parse_verdicts(broken) == {1: ("unsupported", ""), 2: ("supported", "")}
    assert parse_verdicts("无法判断") == {} and parse_verdicts(None) == {}


def test_check_grounding_rules():
    calls = []

    async def judge(payload):
        calls.append(payload)
        return verdicts(True)

    claims = split_claims("- 一般患者应降到140/90以下 [cite:bp_targets]\n- 引用了别的资料 [cite:other]")
    result = asyncio.run(check_grounding(claims, {"bp_targets": SOURCE, "lipids": "血脂"}, judge))
    assert [claim.text for claim in result.unsupported] == ["引用了别的资料"]
    # 只发送被引用的资料
    assert list(calls[0]["资料"]) == ["bp_targets"]

    calls.clear()
    result = asyncio.run(check_grounding(split_claims("长期饮酒会伤肝。"), {}, judge))
    assert calls == [] and len(result.unsupported) == 1  # 没有任何可对照内容，直接判不支持

    async def silent(payload):
        return '{"results": []}'

    result = asyncio.run(check_grounding(split_claims(GOOD), {"bp_targets": SOURCE}, silent))
    assert len(result.unsupported) == 2  # 判定缺失按不支持处理


# ---------- 问答节点 ----------

def test_supported_answer_passes_unchanged():
    llm, judge = Scripted(GOOD), Scripted(verdicts(True, True))
    update = answer(llm, judge)
    assert update["reply"].startswith(GOOD)
    assert len(llm.seen) == 1 and len(judge.seen) == 1
    assert update["event_data"]["grounding"] == {"checked": 2, "unsupported": [], "removed": 0, "rewritten": False}
    sent = payloads(judge)[0]
    assert sent["用户提供的信息"]["问题"] == "高血压要降到多少" and sent["资料"] == {"bp_targets": SOURCE}


def test_unsupported_sentence_triggers_one_rewrite():
    llm, judge = Scripted(BAD, GOOD), Scripted(verdicts(True, True, False), verdicts(True, True))
    update = answer(llm, judge)
    assert "孕期" not in update["reply"] and update["reply"].startswith(GOOD)
    assert "部分常用降压药在孕期禁用" in llm.seen[1][-1].content  # 把不支持的句子告诉模型
    grounding = update["event_data"]["grounding"]
    assert grounding["rewritten"] is True and grounding["removed"] == 0
    assert grounding["initial_unsupported"] == ["部分常用降压药在孕期禁用"]


def test_still_unsupported_after_rewrite_is_removed():
    llm, judge = Scripted(BAD, BAD), Scripted(verdicts(True, True, False), verdicts(True, True, False))
    update = answer(llm, judge)
    assert "孕期" not in update["reply"] and update["reply"].startswith(GOOD)
    grounding = update["event_data"]["grounding"]
    assert grounding["removed"] == 1 and grounding["unsupported"] == ["部分常用降压药在孕期禁用"]


def test_nothing_left_refuses():
    llm, judge = Scripted(GOOD, GOOD), Scripted(verdicts(False, False), verdicts(False, False))
    update = answer(llm, judge)
    assert update["reply"].startswith(NO_EVIDENCE)
    assert update["event_data"]["grounding_refused"] is True and update["event_data"]["no_evidence"] is True


def test_rewrite_failure_still_removes_from_original():
    class FailingRewrite(Scripted):
        async def ainvoke(self, messages):
            if self.seen:
                raise TimeoutError("slow")
            return await super().ainvoke(messages)

    update = answer(FailingRewrite(BAD), Scripted(verdicts(True, True, False)))
    assert "孕期" not in update["reply"] and update["event_data"]["grounding"]["removed"] == 1


def test_judge_failure_or_absence_is_marked_not_passed(caplog):
    update = answer(Scripted(BAD), Scripted(error=PermissionError("secret-in-message")))
    data = update["event_data"]
    assert data["grounding_skipped"] is True and data["grounding_skip_reason"] == "PermissionError"
    assert "secret-in-message" not in caplog.text

    llm = Scripted(BAD)
    data = answer(llm, None)["event_data"]
    assert data["grounding_skip_reason"] == "no_judge" and len(llm.seen) == 1


def test_mock_answer_is_marked_skipped():
    nodes = Nodes(FakeMCP(), InMemoryStore(), FakeKB(), None, True)
    data = asyncio.run(nodes.answer({"text": "高血压要降到多少", "profile": {}, "messages": []}))["event_data"]
    assert data["grounding_skipped"] is True and data["grounding_skip_reason"] == "mock"


# ---------- 建议节点（verify） ----------

RETRIEVED = [{"id": "bp_targets", "title": "降压目标", "source": "《指南》", "text": SOURCE}]


def verify(judge, advice, attempt=1, retry=False):
    nodes = Nodes(None, None, None, Scripted(), False, judge=judge)
    state = {"retrieved": RETRIEVED, "advice": advice, "attempt": attempt, "grounding_retry": retry,
             "assessment": {"cvd_risk": {"risk_percent": 12.3}}, "profile": {"age": 55}}
    return asyncio.run(nodes.verify(state))


def test_advice_first_failure_asks_advise_to_rewrite():
    update = verify(Scripted(verdicts(True, True, False)), BAD)
    data = update["event_data"]
    assert data["ok"] is False and update["grounding_retry"] is True
    assert any("部分常用降压药在孕期禁用" in problem for problem in data["problems"])
    assert update["advice"] == BAD  # 先不删句，交给 advise 重写


def test_advice_second_failure_removes_sentence():
    judge = Scripted(verdicts(True, True, False))
    update = verify(judge, BAD, attempt=2, retry=True)
    data = update["event_data"]
    assert data["ok"] is True and update["advice"] == GOOD
    assert data["grounding"]["removed"] == 1 and data["grounding"]["rewritten"] is True
    assert payloads(judge)[0]["工具结果"] == {"cvd_risk": {"risk_percent": 12.3}}


def test_advice_emptied_falls_back_to_source_text():
    update = verify(Scripted(verdicts(False, False)), GOOD, attempt=3)
    assert update["advice"] == template_advice(RETRIEVED)
    assert update["event_data"]["fallback"] is True and update["event_data"]["grounding_fallback"] is True


def test_template_advice_is_not_judged():
    judge = Scripted()
    update = verify(judge, template_advice(RETRIEVED))
    assert judge.seen == [] and update["event_data"]["grounding_skip_reason"] == "template"


def test_grounding_is_a_valid_model_route(monkeypatch):
    monkeypatch.setenv("QWEN_MODEL_ROUTES", json.dumps({"grounding": "judge-model"}))
    assert ModelGateway(Scripted(), "main").routes == {"grounding": "judge-model"}


@pytest.mark.parametrize("text", ["## 标题", "> 仅为技术演示，不构成医疗建议。", "资料没有涉及孕妇。", "以下是建议：", "请尽快咨询医生。"])
def test_meta_sentences_are_not_judged(text):
    assert all(claim.meta for claim in split_claims(text))
