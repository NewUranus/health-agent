"""B4 PostgreSQL 权限过滤、撤回持久化和原子回滚验收。"""
import asyncio
import pytest
from app.knowledge import PgKnowledgeBase
from app.knowledge_access import knowledge_scope
from app.knowledge_review import preview, apply_review
from test_postgres import pg
from test_governance import knowledge_scenario


@pytest.mark.parametrize("vector", [False, True])
def test_pg_access_before_ranking_and_withdrawal_survives_restart(pg, vector):
    database, _, user, *_ = pg
    async def scenario():
        kb = PgKnowledgeBase(database.url, embedder=(lambda texts: [[1, 0] for _ in texts]) if vector else None)
        await kb.setup()
        await knowledge_scenario(kb, user)
        restarted = PgKnowledgeBase(database.url)
        await restarted.setup()
        docs = await restarted.list_documents()
        private = next(row for row in docs if row["title"] == "私有测试")
        assert private["withdrawn"] and private["visibility"] == "restricted"
        with knowledge_scope("account:" + user):
            assert await restarted.search("血压", document_id=private["id"]) == []
    asyncio.run(scenario())


def test_pg_policy_and_withdrawal_rollback_when_finalize_fails(pg):
    database, _, user, *_ = pg
    async def scenario():
        kb = PgKnowledgeBase(database.url)
        await kb.setup()
        doc = await kb.create_draft("回滚测试", "虚构", "v1", [{"seq": 0, "text": "血压测试"}], "test.pdf")
        await kb.publish(doc["id"])
        async def failed(conn, result):
            raise RuntimeError("模拟审批审计提交失败")
        for action, policy in (("withdraw", None), ("access", {"visibility": "restricted", "allowed_user_ids": [user]})):
            fingerprint, _ = await preview(kb, doc["id"], action, policy)
            with pytest.raises(RuntimeError):
                await apply_review(kb, doc["id"], action, fingerprint, failed, access=policy)
            current = await kb.get_document(doc["id"])
            assert current["visibility"] == "shared" and not current["withdrawn"] and current["revision"] == 1
    asyncio.run(scenario())
