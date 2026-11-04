"""B3 单级确认与失败任务处理验收，全部使用虚构资料。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from app.auth import digest
from test_foundation import secured, login, headers

ADMIN = {"Authorization": "Bearer local-admin-test"}


def draft(client, title="确认测试资料", text="测试文本：发布前核对数字和适用范围。"):
    return client.portal.call(client.app.state.agent.kb.create_draft, title, "测试来源", "v1",
                              [{"seq": 0, "page": 1, "heading": "测试", "text": text}], "test.pdf")


def proposal(client, document, action="publish"):
    response = client.post("/api/v1/admin/approvals", headers=ADMIN,
                           json={"document_id": document["id"], "action": action})
    assert response.status_code == 201, response.text
    return response.json()


def decide(client, approval, decision="approve", prefix="/api/v1"):
    return client.post(f'{prefix}/admin/approvals/{approval["id"]}/decision', headers=ADMIN,
                       json={"decision": decision, "fingerprint": approval["fingerprint"]})


def test_formal_admin_requires_approval_and_duplicate_decision_is_idempotent(secured):
    client, database, _ = secured
    document = draft(client)
    for prefix in ("/api", "/api/v1"):
        assert client.post(f'{prefix}/admin/documents/{document["id"]}/publish', headers=ADMIN).status_code == 428
        assert client.delete(f'{prefix}/admin/documents/{document["id"]}', headers=ADMIN).status_code == 428
        assert client.post(prefix + "/admin/approvals", json={"document_id": document["id"], "action": "publish"}).status_code == 401
    approval = proposal(client, document)
    assert approval["status"] == "waiting_human" and approval["summary"]["chunk_count"] == 1
    result = decide(client, approval)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "succeeded" and result.json()["result"]["status"] == "published"
    again = decide(client, approval, prefix="/api")
    assert again.status_code == 200 and again.json()["result"] == result.json()["result"]
    assert decide(client, approval, "reject").status_code == 409
    with database.connection() as execute:
        rows = execute("SELECT action FROM hp_audit WHERE resource = ?", (approval["id"],)).fetchall()
    assert [row["action"] for row in rows].count("approval.succeeded") == 1
    assert [row["action"] for row in rows].count("approval.approve") == 1


def test_rejection_expiry_wrong_fingerprint_and_credential_rotation(secured, monkeypatch):
    client, database, _ = secured
    document = draft(client)
    rejected = proposal(client, document)
    assert decide(client, rejected, "reject").json()["status"] == "rejected"
    assert decide(client, rejected, "reject").status_code == 200
    assert decide(client, rejected).status_code == 409
    expired = proposal(client, document)
    with database.connection() as execute:
        execute("UPDATE hp_approvals SET expires_at = 0 WHERE id = ?", (expired["id"],))
    assert decide(client, expired).json()["code"] == "APPROVAL_EXPIRED"
    fresh = proposal(client, document)
    assert decide(client, {**fresh, "fingerprint": "0" * 64}).json()["code"] == "APPROVAL_MISMATCH"
    monkeypatch.setenv("ADMIN_PASSWORD", "rotated-admin-password")
    assert decide(client, fresh).status_code == 401
    rotated = client.get(f'/api/admin/approvals/{fresh["id"]}', headers={"Authorization": "Bearer rotated-admin-password"})
    assert rotated.status_code == 404
    assert client.portal.call(client.app.state.agent.kb.get_document, document["id"])["status"] == "draft"


def test_changed_content_and_changed_replacement_set_invalidate_approval(secured):
    client, _, _ = secured
    document = draft(client)
    approval = proposal(client, document)
    client.patch(f'/api/admin/documents/{document["id"]}/chunks/{document["chunks"][0]["id"]}', headers=ADMIN, json={"text": "修改后的新资料。"})
    assert decide(client, approval).json()["code"] == "APPROVAL_STALE"
    old = draft(client)
    client.portal.call(client.app.state.agent.kb.publish, old["id"])
    approval = proposal(client, document)
    assert approval["summary"]["replaced"][0]["id"] == old["id"]
    client.portal.call(client.app.state.agent.kb.delete_document, old["id"])
    assert decide(client, approval).json()["code"] == "APPROVAL_STALE"
    assert client.portal.call(client.app.state.agent.kb.get_document, document["id"])["status"] == "draft"


def test_delete_confirmation_and_builtin_protection(secured):
    client, _, _ = secured
    document = draft(client)
    approval = proposal(client, document, "delete")
    assert decide(client, approval).json()["status"] == "succeeded"
    assert decide(client, approval).status_code == 200
    assert client.get(f'/api/admin/documents/{document["id"]}', headers=ADMIN).status_code == 404
    builtin = next(row for row in client.get("/api/admin/documents", headers=ADMIN).json()["documents"] if row["builtin"])
    assert client.post("/api/admin/approvals", headers=ADMIN, json={"document_id": builtin["id"], "action": "delete"}).status_code == 409


def test_concurrent_approvals_and_execution_revalidation(secured, monkeypatch):
    client, database, _ = secured
    document = draft(client)
    approval = proposal(client, document)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: decide(client, approval), range(4)))
    assert all(response.status_code in (200, 409) for response in responses)
    assert any(response.status_code == 200 for response in responses)
    kb = client.app.state.agent.kb
    fresh = draft(client, "权限变更资料")
    second = proposal(client, fresh)
    original = kb.publication_vectors
    async def changed(doc):
        result = await original(doc)
        monkeypatch.setenv("ADMIN_PASSWORD", "changed-during-execution")
        return result
    monkeypatch.setattr(kb, "publication_vectors", changed)
    assert decide(client, second).status_code == 401
    assert client.portal.call(kb.get_document, fresh["id"])["status"] == "draft"
    with database.connection() as execute:
        assert execute("SELECT status FROM hp_approvals WHERE id = ?", (second["id"],)).fetchone()["status"] == "invalidated"


def test_expiry_during_execution_and_failed_embedding_keep_draft(secured, monkeypatch):
    client, database, _ = secured
    kb = client.app.state.agent.kb
    document = draft(client)
    approval = proposal(client, document)
    original = kb.publication_vectors
    async def expired(doc):
        result = await original(doc)
        with database.connection() as execute:
            execute("UPDATE hp_approvals SET expires_at = 0 WHERE id = ?", (approval["id"],))
        return result
    monkeypatch.setattr(kb, "publication_vectors", expired)
    assert decide(client, approval).json()["code"] == "APPROVAL_EXPIRED"
    assert client.portal.call(kb.get_document, document["id"])["status"] == "draft"
    async def failed(doc):
        raise RuntimeError("仅测试内部故障，不应返回客户端")
    monkeypatch.setattr(kb, "publication_vectors", failed)
    failed_approval = proposal(client, document)
    response = decide(client, failed_approval)
    assert response.json()["code"] == "APPROVAL_EXECUTION_FAILED"
    assert "内部故障" not in response.text
    assert client.portal.call(kb.get_document, document["id"])["status"] == "draft"


def test_failure_review_is_scoped_audited_and_does_not_retry(secured):
    client, database, users = secured
    store = client.app.state.task_store
    alice, bob = users["alice"]["id"], users["bob"]["id"]
    task = store.submit(alice, "cancel-for-review", {"thread_id": "review", "message": "测试"}, "request")[0]
    store.cancel(task["id"], alice, "cancel")
    _, token = login(client, "alice")
    path = f'/api/tasks/{task["id"]}/review'
    result = client.post(path, headers=headers(token), json={"disposition": "needs_support"})
    assert result.status_code == 200 and result.json()["review_disposition"] == "needs_support"
    assert result.json()["status"] == "cancelled" and result.json()["failure_category"] == "cancelled"
    assert client.post(path, headers=headers(token), json={"disposition": "needs_support"}).status_code == 200
    rows = client.get("/api/v1/tasks?failed=true", headers=headers(token)).json()["tasks"]
    assert [row["id"] for row in rows] == [task["id"]]
    _, other = login(client, "bob")
    assert client.post(path, headers=headers(other), json={"disposition": "resolved"}).status_code == 404
    assert client.get("/api/tasks?failed=true", headers=headers(other)).json()["tasks"] == []
    with database.connection() as execute:
        rows = execute("SELECT action FROM hp_audit WHERE resource = ? AND action = 'task.review.needs_support'", (task["id"],)).fetchall()
    assert len(rows) == 1 and store.get(task["id"], alice)["status"] == "cancelled"


def test_memory_confirmation_cancellation_keeps_commit_and_memory_consistent():
    from app.knowledge import MemoryKnowledgeBase
    from app.knowledge_review import preview, apply_review
    async def scenario():
        kb = MemoryKnowledgeBase()
        document = await kb.create_draft("取消边界测试", "测试", "v1", [{"seq": 0, "text": "测试资料"}], "test.pdf")
        fingerprint, _ = await preview(kb, document["id"], "publish")
        entered, release = asyncio.Event(), asyncio.Event()
        committed = []
        async def finalize(conn, result):
            entered.set()
            await release.wait()
            committed.append(result)
        task = asyncio.create_task(apply_review(kb, document["id"], "publish", fingerprint, finalize))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert committed and (await kb.get_document(document["id"]))["status"] == "published"
    asyncio.run(scenario())
