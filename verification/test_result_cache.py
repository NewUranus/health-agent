"""风险计算和查询向量写入数据库后，新进程能复用；网页搜索不进入缓存。"""
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.knowledge import KnowledgeBase
from app.mcp_client import MCPClient
from app.result_cache import ResultCache


class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Session:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, name, args, meta=None):
        self.calls += 1
        await asyncio.sleep(0.02)
        return type("Response", (), {"is_error": False, "content": [_Text(json.dumps({"result": {"tool": name, "n": args["n"]}}))]})()


class _Model:
    def __init__(self, model):
        self.model = model
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


def _client(session, cache):
    client = MCPClient()
    client.session = session
    client.results = cache
    return client


def test_tool_cache_is_shared_and_web_results_are_not(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    session = _Session()

    async def run():
        first = ResultCache()
        first.setup()
        second = ResultCache()
        one, two = _client(session, first), _client(session, second)
        missed, hit = await asyncio.gather(one.call_with_trace("cvd_risk", {"n": 1}), one.call_with_trace("cvd_risk", {"n": 1}))
        assert session.calls == 1
        assert {missed["cached"], hit["cached"]} == {False, True}
        reused = await two.call_with_trace("cvd_risk", {"n": 1})
        assert reused["cached"] is True and reused["result"]["n"] == 1 and session.calls == 1
        await one.call_with_trace("search_web", {"n": 1})
        await two.call_with_trace("search_web", {"n": 1})
        assert session.calls == 3
        return first.stats()["tool"]

    stats = asyncio.run(run())
    assert stats == {"hits": 2, "misses": 1, "saved": 2}


def test_query_vectors_follow_the_embedding_model(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "vectors.sqlite3"))
    monkeypatch.setattr("app.result_cache.ROW_LIMIT", 1)
    model, other = _Model("unit-model"), _Model("other-model")

    async def run():
        cache = ResultCache()
        cache.setup()
        first = KnowledgeBase(model.embed_documents)
        first.results = cache
        again = KnowledgeBase(model.embed_documents)
        again.results = ResultCache()
        changed = KnowledgeBase(other.embed_documents)
        changed.results = cache
        original = await first.query_vector("减盐")
        repeated = await again.query_vector("减盐")
        separate = await changed.query_vector("减盐")
        cache.write("tool", "old", {"n": 1})
        cache.write("tool", "new", {"n": 2})
        return original, repeated, separate, cache.read("tool", "old"), cache.read("tool", "new"), cache.stats()["vector"]

    original, repeated, separate, old, new, stats = asyncio.run(run())
    assert original == repeated == [2.0, 1.0]
    assert separate == [2.0, 1.0]
    assert model.calls == 1 and other.calls == 1
    assert old is None and new == {"n": 2}
    assert stats == {"hits": 1, "misses": 2, "saved": 1}
