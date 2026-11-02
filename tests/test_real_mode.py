"""真实模式的分支：用脚本化模型，不联网。"""

import asyncio
import json
import logging

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.store.memory import InMemoryStore

from app.graph import Nodes
from app.llm import make_llm
from app.rag import Chunk


class FakeLLM:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return AIMessage(content=self.content)


def intake(llm, text):
    nodes = Nodes(None, InMemoryStore(), None, llm, False)
    return asyncio.run(nodes.intake({"user_id": "u", "text": text}))


FULL = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"


def test_extract_skipped_when_rules_are_enough():
    llm = FakeLLM(content="{}")
    intake(llm, FULL)
    assert llm.calls == 0


def test_extract_fills_what_rules_missed():
    llm = FakeLLM(content=json.dumps({"age": 50, "sex": "male"}))
    update = intake(llm, "我是五十岁的老爷们")
    assert llm.calls == 1
    assert update["profile"] == {"age": 50, "sex": "male"}


def test_failure_is_logged_and_reported(caplog):
    with caplog.at_level(logging.WARNING):
        update = intake(FakeLLM(error=PermissionError("secret-in-message")), "你好")
    data = update["event_data"]
    assert data["degraded"] is True
    assert data["degraded_reason"] == "PermissionError"
    assert "PermissionError" in caplog.text
    assert "secret-in-message" not in caplog.text


def test_thinking_mode_is_off(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "x")
    assert make_llm().extra_body == {"enable_thinking": False}


# ---------- 问答校验失败的处理 ----------


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return AIMessage(content=self.replies.pop(0))


class FakeKB:
    async def search(self, query, k=4, min_similarity=None):
        return [Chunk("bp_targets", "降压目标", "《指南》", "一般高血压患者应将血压降至140/90以下", 0.6)]


class FakeMCP:
    async def list_tools(self):
        return []


def answer(llm, history=()):
    nodes = Nodes(FakeMCP(), InMemoryStore(), FakeKB(), llm, False)
    return asyncio.run(nodes.answer({"text": "高血压孕妇能吃什么药", "profile": {}, "messages": list(history)}))


def test_valid_answer_is_used_without_retry():
    llm = ScriptedLLM("- 血压目标见资料 [cite:bp_targets]")
    update = answer(llm)
    assert "[cite:bp_targets]" in update["reply"]
    assert len(llm.seen) == 1
    assert "fallback" not in update["event_data"]


def test_invalid_answer_is_retried_with_the_problem():
    llm = ScriptedLLM("- 见资料 [cite:healthy_diet]", "- 血压目标见资料 [cite:bp_targets]")
    update = answer(llm)
    assert "[cite:bp_targets]" in update["reply"]
    assert len(llm.seen) == 2
    assert "healthy_diet" in llm.seen[1][-1].content  # 把校验问题告诉模型
    assert "fallback" not in update["event_data"]


def test_second_failure_refuses_and_never_dumps_sources():
    llm = ScriptedLLM("资料未覆盖孕妇，无法回答。", "资料未覆盖孕妇，无法回答。")
    update = answer(llm)
    data = update["event_data"]
    assert data["fallback"] is True and data["no_evidence"] is True and data["problems"]
    assert "无法回答" in update["reply"] and "咨询医生" in update["reply"]
    assert "140/90" not in update["reply"]  # 不能把普通成人的内容塞给孕妇
    assert "[cite:" not in update["reply"]


def test_old_citations_are_removed_from_history():
    history = [HumanMessage("上一个问题"), AIMessage("- 多吃蔬菜 [cite:healthy_diet]")]
    llm = ScriptedLLM("- 血压目标见资料 [cite:bp_targets]")
    answer(llm, history)
    sent = "\n".join(str(m.content) for m in llm.seen[0])
    assert "healthy_diet" not in sent and "多吃蔬菜" in sent
