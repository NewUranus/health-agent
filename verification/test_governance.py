"""B4 权限、撤回与模型调用保护的独立验收；仅使用虚构资料。"""
import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from app.knowledge import MemoryKnowledgeBase
from app.knowledge_access import knowledge_scope
from app.knowledge_review import preview, apply_review
from app.model_gateway import ModelGateway, ModelUnavailable, model_run, model_scene, save_usage
from test_foundation import secured, login, headers
from test_approvals import ADMIN, draft, proposal, decide


async def apply(kb, ident, action, access=None):
    fingerprint, _ = await preview(kb, ident, action, access)
    async def finalize(conn, result):
        pass
    return await apply_review(kb, ident, action, fingerprint, finalize, access=access)


async def knowledge_scenario(kb, user):
    private = await kb.create_draft("私有测试", "虚构", "v1", [{"seq": 0, "text": "血压 运动 私有金丝雀"}], "test.pdf")
    public = await kb.create_draft("共享测试", "虚构", "v2", [{"seq": 0, "text": "血压 运动 共享资料"}], "test.pdf")
    for document in (private, public):
        await kb.publish(document["id"])
    await apply(kb, private["id"], "access", {"visibility": "restricted", "allowed_user_ids": [user]})
    with knowledge_scope("account:" + user):
        private_hits = [asdict(row) for row in await kb.search("血压", 10) if row.document_id == private["id"]]
        assert private_hits and private_hits[0]["version"] == "v1"
        assert await kb.valid_evidence(private_hits) == private_hits
    for principal in ("", "account:" + "f" * 32):
        with knowledge_scope(principal):
            assert [row.document_id for row in await kb.search("血压", 1)] == [public["id"]]
            assert await kb.search("血压", 1, document_id=private["id"]) == []
            assert await kb.valid_evidence(private_hits) == []
    with knowledge_scope(None):
        assert len(await kb.search("血压", 10)) == 2
    await apply(kb, private["id"], "withdraw")
    assert (await kb.get_document(private["id"]))["chunks"][0]["text"] == "血压 运动 私有金丝雀"
    with knowledge_scope("account:" + user):
        assert await kb.valid_evidence(private_hits) == []
    events = [{"type": "node", "data": {"chunks": private_hits}},
              {"type": "message", "content": "私有金丝雀 [cite:" + private_hits[0]["id"] + "]"}]
    filtered = await kb.sanitize_events(events, "account:" + user)
    assert "私有金丝雀" not in json.dumps(filtered, ensure_ascii=False)
    assert "隐藏" in filtered[-1]["content"]
    web = {"id": "web-public-1", "text": "公开网页", "origin": "web"}
    assert await kb.valid_evidence([web]) == [web]
    assert (await kb.sanitize_events([{"type": "message", "content": "[cite:web-public-1]"}], ""))[0]["content"] == "[cite:web-public-1]"


@pytest.mark.parametrize("vector", [False, True])
def test_memory_access_before_ranking_and_withdrawal(vector):
    kb = MemoryKnowledgeBase(embedder=(lambda texts: [[1, 0] for _ in texts]) if vector else None)
    asyncio.run(knowledge_scenario(kb, "a" * 32))


def access_proposal(client, document, policy):
    return client.post("/api/v1/admin/approvals", headers=ADMIN,
                       json={"action": "access", "document_id": document["id"], "access": policy})


def test_access_requires_approval_and_history_revalidates_both_routes(secured):
    client, database, users = secured
    doc = draft(client, "隔离资料", "虚构金丝雀：血压运动测试")
    decide(client, proposal(client, doc))
    kb = client.app.state.agent.kb
    policy = {"visibility": "restricted", "allowed_user_ids": [users["alice"]["id"]]}
    response = access_proposal(client, doc, policy)
    assert response.status_code == 201, response.text
    assert client.get(f'/api/admin/documents/{doc["id"]}', headers=ADMIN).json()["visibility"] == "shared"
    approval = response.json()
    assert decide(client, approval).json()["result"]["visibility"] == "restricted"
    assert decide(client, approval).json()["status"] == "succeeded"
    async def get_hits():
        with knowledge_scope("account:" + users["alice"]["id"]):
            return [asdict(row) for row in await kb.search("虚构金丝雀", 10, document_id=doc["id"])]
    chunks = client.portal.call(get_hits)
    assert chunks
    store = client.app.state.task_store
    task = store.submit(users["alice"]["id"], "governance-history", {"thread_id": "governance", "message": "测试"}, "request")[0]
    claimed = store.claim("worker")
    assert claimed["id"] == task["id"]
    store.append(task["id"], "worker", {"type": "node", "data": {"chunks": chunks}})
    store.append(task["id"], "worker", {"type": "message", "content": "虚构金丝雀 [cite:" + chunks[0]["id"] + "]"})
    store.finish(task["id"], "worker", "succeeded")
    _, token = login(client, "alice")
    assert "虚构金丝雀" in client.get(f'/api/tasks/{task["id"]}/event-page', headers=headers(token)).text
    removal = access_proposal(client, doc, {"visibility": "restricted", "allowed_user_ids": []}).json()
    assert decide(client, removal).status_code == 200
    for prefix in ("/api", "/api/v1"):
        for path in ("event-page", "events"):
            response = client.get(f'{prefix}/tasks/{task["id"]}/{path}', headers=headers(token))
            assert response.status_code == 200 and "虚构金丝雀" not in response.text
    withdraw = proposal(client, doc, "withdraw")
    assert decide(client, withdraw).json()["result"]["withdrawn"] is True
    assert client.get(f'/api/admin/documents/{doc["id"]}', headers=ADMIN).json()["chunks"]
    # 上面已登录普通账号，会话还在；后台应拒绝该角色，而不是当成未登录。
    denied = client.get("/api/admin/accounts")
    assert denied.status_code == 403 and denied.json()["code"] == "ACCESS_DENIED"
    assert len(client.get("/api/admin/accounts", headers=ADMIN).json()["accounts"]) == 2


def test_policy_validation_and_stale_approval(secured):
    client, _, users = secured
    doc = draft(client)
    for policy in ({"visibility": "restricted", "allowed_user_ids": ["wrong"]},
                   {"visibility": "shared", "allowed_user_ids": [users["alice"]["id"]]}):
        assert access_proposal(client, doc, policy).status_code == 422
    assert access_proposal(client, doc, {"visibility": "restricted", "allowed_user_ids": ["f" * 32]}).status_code == 422
    first = proposal(client, doc)
    access = access_proposal(client, doc, {"visibility": "restricted", "allowed_user_ids": []}).json()
    assert decide(client, access).status_code == 200
    assert decide(client, first).json()["code"] == "APPROVAL_STALE"


class Provider:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
    def bind_tools(self, tools):
        return self
    async def ainvoke(self, messages):
        self.calls += 1
        if self.fail:
            raise RuntimeError("秘密正文和密钥不应进入指标")
        return SimpleNamespace(content="测试回答", usage_metadata={"input_tokens": 8, "output_tokens": 2})


def test_gateway_budget_fallback_circuit_and_private_metrics(monkeypatch):
    monkeypatch.setenv("HEALTH_AGENT_LLM_MAX_CALLS", "2")
    async def scenario():
        primary, fallback = Provider(True), Provider()
        gateway = ModelGateway(primary, "primary", fallback, "fallback")
        for _ in range(3):
            with model_run() as run, model_scene("answer:test-v1"):
                response = await gateway.bind_tools([]).ainvoke([HumanMessage("私密问题")])
                assert response.content == "测试回答" and run.calls == 2
                with pytest.raises(ModelUnavailable):
                    await gateway.ainvoke([HumanMessage("再次调用")])
                assert "私密" not in str(run.records) and "密钥" not in str(run.records)
                assert run.records[0]["status"] == "failed" and run.records[1]["input_tokens"] == 8
        with model_run() as run:
            await gateway.ainvoke([HumanMessage("熔断期间")])
            assert run.calls == 1 and primary.calls == 3 and fallback.calls == 4
        with model_run() as run:
            with pytest.raises(ModelUnavailable):
                await gateway.ainvoke([HumanMessage("字" * 60001)])
            assert run.calls == 0
    asyncio.run(scenario())


def test_gateway_concurrency_and_cancellation_release(monkeypatch):
    monkeypatch.setenv("HEALTH_AGENT_LLM_CONCURRENCY", "1")
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Slow(Provider):
            async def ainvoke(self, messages):
                entered.set()
                await release.wait()
                return await super().ainvoke(messages)
        gateway = ModelGateway(Slow(), "slow")
        task = asyncio.create_task(gateway.ainvoke([HumanMessage("测试")]))
        await entered.wait()
        with model_run() as run:
            with pytest.raises(ModelUnavailable):
                await gateway.ainvoke([HumanMessage("排队超时")])
            assert run.records[0]["status"] == "busy" and not gateway.control.open_until
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await gateway.ainvoke([HumanMessage("取消后可继续")])).content == "测试回答"
    asyncio.run(scenario())


def test_usage_storage_admin_summary_and_missing_counts(secured):
    client, database, users = secured
    record = {"model": "test", "scene": "answer:v1", "status": "succeeded", "input_tokens": 8, "output_tokens": 2, "duration_ms": 5}
    save_usage("account:" + users["alice"]["id"], "task-test", "request-test", [record, {**record, "input_tokens": None}])
    for prefix in ("/api", "/api/v1"):
        assert client.get(prefix + "/admin/model-usage").status_code == 401
        result = client.get(prefix + "/admin/model-usage", headers=ADMIN).json()
        assert result["currency_cost"] is None
        assert result["groups"][0]["calls"] == 2 and result["groups"][0]["unknown_usage_calls"] == 1
    with database.connection() as execute:
        row = dict(execute("SELECT * FROM hp_model_usage LIMIT 1").fetchone())
    assert row["user_id"] == users["alice"]["id"] and row["task_id"] == "task-test"
    assert "content" not in row and "prompt" not in row


def test_revoked_history_cannot_enter_model_even_when_model_omitted_citation():
    from langchain_core.messages import AIMessage
    from app.knowledge_access import evidence_refs
    async def scenario():
        kb = MemoryKnowledgeBase()
        doc = await kb.create_draft("历史金丝雀", "虚构", "v1", [{"seq": 0, "text": "私有旧结论"}], "test.pdf")
        await kb.publish(doc["id"])
        chunks = [asdict(row) for row in await kb.search("私有旧结论")]
        history = [HumanMessage("问题"), AIMessage("没有引用但包含私有旧结论", additional_kwargs={"knowledge_evidence": evidence_refs(chunks)})]
        assert len(await kb.visible_history(history)) == 2
        assert "knowledge_evidence" in (await kb.visible_history(history))[-1].additional_kwargs
        await apply(kb, doc["id"], "access", {"visibility": "restricted", "allowed_user_ids": []})
        assert len(await kb.visible_history(history)) == 1
        events = [{"type": "message", "content": "没有引用但包含私有旧结论", "evidence": evidence_refs(chunks)}]
        assert "私有旧结论" not in (await kb.sanitize_events(events, ""))[0]["content"]
        trace = {"type": "node", "data": {"tool_calls": [{"args": {"bad": "私有旧结论"}}]}, "evidence": evidence_refs(chunks)}
        assert "私有旧结论" not in json.dumps(await kb.sanitize_events([trace], ""), ensure_ascii=False)
    asyncio.run(scenario())


def test_revocation_before_fallback_prevents_second_provider_receiving_content():
    from app.model_gateway import model_evidence, EvidenceChanged
    async def scenario():
        kb = MemoryKnowledgeBase()
        doc = await kb.create_draft("撤权测试", "虚构", "v1", [{"seq": 0, "text": "私有依据"}], "test.pdf")
        await kb.publish(doc["id"])
        chunks = [asdict(row) for row in await kb.search("私有依据")]
        class Revoking(Provider):
            async def ainvoke(self, messages):
                await apply(kb, doc["id"], "withdraw")
                raise TimeoutError("模拟首次调用超时")
        fallback = Provider()
        with model_run() as run, model_evidence(kb, chunks):
            with pytest.raises(EvidenceChanged):
                await ModelGateway(Revoking(), "primary", fallback, "fallback").ainvoke([HumanMessage("私有依据")])
        assert fallback.calls == 0 and run.records[-1]["status"] == "access_changed"
    asyncio.run(scenario())


def test_answer_inherits_history_evidence_for_future_revocation():
    from app.graph import Nodes
    from app.knowledge_access import evidence_refs
    from langchain_core.messages import AIMessage
    async def scenario():
        kb = MemoryKnowledgeBase()
        old = await kb.create_draft("历史依据", "虚构", "v1", [{"seq": 0, "text": "旧金丝雀"}], "old.pdf")
        new = await kb.create_draft("当前依据", "虚构", "v1", [{"seq": 0, "text": "当前测试关键词"}], "new.pdf")
        for doc in (old, new):
            await kb.publish(doc["id"])
        old_chunks = [asdict(row) for row in await kb.search("旧金丝雀")]
        previous = AIMessage("旧结论", additional_kwargs={"knowledge_evidence": evidence_refs(old_chunks)})
        class FakeMCP:
            async def list_tools(self):
                return []
        class Reply(Provider):
            async def ainvoke(self, messages):
                assert all("knowledge_evidence" not in message.additional_kwargs for message in messages)
                return AIMessage("根据旧结论补充回答 [cite:" + new["chunks"][0]["id"] + "]")
        node = Nodes(FakeMCP(), None, kb, ModelGateway(Reply(), "test"), False)
        update = await node.answer({"text": "当前测试关键词", "profile": {}, "messages": [previous]})
        assert old["chunks"][0]["id"] in {ref["id"] for ref in update["reply_evidence"]}
        await apply(kb, old["id"], "withdraw")
        assert not await kb.valid_references(update["reply_evidence"])
        latest = AIMessage(update["reply"], additional_kwargs={"knowledge_evidence": update["reply_evidence"]})
        assert await kb.visible_history([latest]) == []
    asyncio.run(scenario())


def test_live_stream_hides_derived_tool_details_after_revocation(tmp_path, monkeypatch):
    from app.agent import HealthAgent
    from app.knowledge_access import evidence_refs
    monkeypatch.setattr("app.agent.ROOT", tmp_path)
    async def scenario():
        kb = MemoryKnowledgeBase()
        doc = await kb.create_draft("流式撤回", "虚构", "v1", [{"seq": 0, "text": "流式金丝雀"}], "test.pdf")
        await kb.publish(doc["id"])
        refs = evidence_refs([asdict(row) for row in await kb.search("流式金丝雀")])
        await apply(kb, doc["id"], "withdraw")
        class Graph:
            async def aget_state(self, *args, **kwargs):
                return SimpleNamespace()
            async def astream(self, *args, **kwargs):
                yield (), {"answer": {"reply": "流式金丝雀", "reply_evidence": refs,
                    "event_data": {"tool_calls": [{"args": {"bad": "流式金丝雀"}}]}}}
        agent = HealthAgent(mock=True)
        agent.kb, agent.graph = kb, Graph()
        events = [event async for event in agent._stream("test", "问题", "test")]
        assert "流式金丝雀" not in json.dumps(events, ensure_ascii=False)
        assert events[0]["data"]["evidence_unavailable"]
        assert any(event["type"] == "message" and "权限发生变化" in event["content"] for event in events)
    asyncio.run(scenario())
