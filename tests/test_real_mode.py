"""真实模式的分支：用脚本化模型，不联网。"""

import asyncio
import json
import logging

from langchain_core.messages import AIMessage
from langgraph.store.memory import InMemoryStore

from app.graph import Nodes
from app.llm import make_llm


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
