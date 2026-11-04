"""B3 PostgreSQL 知识变更、确认终态与审计的原子提交验收。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import uuid

from app.approvals import ApprovalStore, ApprovalDecision, execute_approval
from app.auth import AuthFailure, digest
from app.knowledge import PgKnowledgeBase
from app.knowledge_review import preview
from test_postgres import pg
import pytest

ADMIN_VALUE = "approval-pg-test-password"
ADMIN = {"Authorization": "Bearer " + ADMIN_VALUE}


def test_pg_approval_mutation_and_audit_rollback_together(pg, monkeypatch):
    database, *_ = pg
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_VALUE)
    approvals = ApprovalStore(database)
    approvals.setup()
    async def scenario():
        kb = PgKnowledgeBase(database.url)
        await kb.setup()
        old = await kb.create_draft("事务测试资料", "测试", "v1", [{"seq": 0, "text": "旧版本测试内容"}], "old.pdf")
        await kb.publish(old["id"])
        new = await kb.create_draft("事务测试资料", "测试", "v2", [{"seq": 0, "text": "新版本测试内容"}], "new.pdf")
        fingerprint, summary = await preview(kb, new["id"], "publish")
        proposal = approvals.create(digest(ADMIN_VALUE), "publish", new["id"], fingerprint, summary, "created")
        request = SimpleNamespace(headers={"authorization": "Bearer " + ADMIN_VALUE}, state=SimpleNamespace(request_id="execution"),
            app=SimpleNamespace(state=SimpleNamespace(auth_required=True, approval_store=approvals, agent=SimpleNamespace(kb=kb))))
        with database.connection() as execute:
            execute("CREATE FUNCTION fail_approval_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.action = 'approval.succeeded' THEN RAISE EXCEPTION 'test audit failure'; END IF; RETURN NEW; END $$")
            execute("CREATE TRIGGER fail_approval_audit BEFORE INSERT ON hp_audit FOR EACH ROW EXECUTE FUNCTION fail_approval_audit()")
        with pytest.raises(AuthFailure, match="操作未完成"):
            await execute_approval(request, proposal["id"], ApprovalDecision(decision="approve", fingerprint=fingerprint))
        assert (await kb.get_document(new["id"]))["status"] == "draft"
        assert (await kb.get_document(old["id"]))["status"] == "published"
        assert approvals.get(proposal["id"], digest(ADMIN_VALUE))["status"] == "failed"
        with database.connection() as execute:
            execute("DROP TRIGGER fail_approval_audit ON hp_audit")
        fresh = approvals.create(digest(ADMIN_VALUE), "publish", new["id"], fingerprint, summary, "retry-preview")
        result = await execute_approval(request, fresh["id"], ApprovalDecision(decision="approve", fingerprint=fingerprint))
        assert result["status"] == "succeeded"
        assert [row["id"] for row in await kb.list_documents()] == [new["id"]]
        repeat = await execute_approval(request, fresh["id"], ApprovalDecision(decision="approve", fingerprint=fingerprint))
        assert repeat["result"] == result["result"]
        with database.connection() as execute:
            rows = execute("SELECT action FROM hp_audit WHERE resource = ? AND action = 'approval.succeeded'", (fresh["id"],)).fetchall()
        assert len(rows) == 1
    asyncio.run(scenario())


def test_pg_api_concurrent_confirmation_and_restart_history(pg, monkeypatch):
    database, tasks, user, spawn, stop, api = pg
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_VALUE)
    server, client = api()
    async def create():
        kb = PgKnowledgeBase(database.url)
        return await kb.create_draft("确认并发测试", "测试", "v1", [{"seq": 0, "text": "并发发布的虚构资料。"}], "test.pdf")
    document = asyncio.run(create())
    proposal = client.post("/api/admin/approvals", headers=ADMIN, json={"action": "publish", "document_id": document["id"]}).json()
    def decide(_):
        return client.post(f'/api/v1/admin/approvals/{proposal["id"]}/decision', headers=ADMIN,
                           json={"decision": "approve", "fingerprint": proposal["fingerprint"]})
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(decide, range(4)))
    assert all(response.status_code in (200, 409) for response in responses)
    assert any(response.status_code == 200 for response in responses)
    client.close()
    stop(server)
    _, client = api()
    result = client.get(f'/api/admin/approvals/{proposal["id"]}', headers=ADMIN).json()
    assert result["status"] == "succeeded" and result["result"]["id"] == document["id"]
    assert client.get(f'/api/admin/documents/{document["id"]}', headers=ADMIN).json()["status"] == "published"
    # 模拟进程退出留下的执行中确认：过期后转待核对，禁止自动再次执行。
    approval_store = ApprovalStore(database)
    delete = client.post("/api/admin/approvals", headers=ADMIN, json={"action": "delete", "document_id": document["id"]}).json()
    approval_store.decide(delete["id"], digest(ADMIN_VALUE), "approve", delete["fingerprint"], "interrupted")
    with database.connection() as execute:
        execute("UPDATE hp_approvals SET expires_at = 0 WHERE id = ?", (delete["id"],))
    assert client.get(f'/api/admin/approvals/{delete["id"]}', headers=ADMIN).json()["status"] == "uncertain"
    response = client.post(f'/api/admin/approvals/{delete["id"]}/decision', headers=ADMIN,
                          json={"decision": "approve", "fingerprint": delete["fingerprint"]})
    assert response.status_code == 409
    assert client.get(f'/api/admin/documents/{document["id"]}', headers=ADMIN).status_code == 200
    client.close()
