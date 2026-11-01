"""短期 checkpointer 与跨会话健康档案 Store。"""
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore


@asynccontextmanager
async def memory_backend():
    url = os.getenv("DATABASE_URL")
    if url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres import AsyncPostgresStore
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            async with AsyncPostgresStore.from_conn_string(url) as store:
                await saver.setup()
                await store.setup()
                yield saver, store
    else:
        yield MemorySaver(), InMemoryStore()


async def load_memory(store, user_id):
    namespace = ("users", user_id)
    profile = await store.aget(namespace, "profile")
    history = await store.aget(namespace, "history")
    return (profile.value if profile else {}), (history.value.get("records", []) if history else [])


async def load_followup(store, user_id):
    plan = await store.aget(("users", user_id), "followup")
    return plan.value if plan else None


async def save_assessment(store, user_id, profile, risk, plan, *, kind="assessment", **details):
    _, records = await load_memory(store, user_id)
    record = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **details,
              "risk_percent": risk["risk_percent"], "level": risk["level"],
              **{key: profile.get(key) for key in ("sbp", "dbp", "total_chol", "hdl", "smoker")}}
    await store.aput(("users", user_id), "profile", dict(profile))
    await store.aput(("users", user_id), "history", {"records": [*records, record]})
    await store.aput(("users", user_id), "followup", plan)
