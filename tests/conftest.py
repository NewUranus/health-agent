import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["HEALTH_AGENT_MOCK"] = "1"


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def chat(client, thread_id: str, message: str) -> list[dict]:
    """发一条消息，返回解析后的 SSE 事件列表。"""
    events = []
    with client.stream("POST", "/api/chat", json={"thread_id": thread_id, "message": message}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


def nodes(events: list[dict]) -> list[str]:
    return [e["node"] for e in events if e["type"] == "node"]


def node_data(events: list[dict], name: str) -> dict:
    found = [e["data"] for e in events if e["type"] == "node" and e["node"] == name]
    assert found, f"没有 {name} 节点事件，实际节点：{nodes(events)}"
    return found[-1]


def reply(events: list[dict]) -> str:
    msgs = [e["content"] for e in events if e["type"] == "message"]
    assert len(msgs) == 1, f"每轮应恰好一个 message 事件，实际 {len(msgs)} 个"
    return msgs[0]
