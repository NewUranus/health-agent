"""短期 checkpointer 与跨会话健康档案 Store。"""
import os
import hashlib
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.store.base import PutOp
from app.task_context import task_execution
from app.database import schema_lock


@asynccontextmanager
async def memory_backend():
    url = os.getenv("DATABASE_URL")
    if url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres import AsyncPostgresStore
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            async with AsyncPostgresStore.from_conn_string(url) as store:
                async with schema_lock(url):
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
    execution = task_execution.get()
    # PostgreSQL 使用独立事务连接；与任务行锁、写入凭据一起提交。
    if os.getenv("DATABASE_URL"):
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        from langgraph.store.postgres import AsyncPostgresStore
        async with await AsyncConnection.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=5) as conn:
            async with conn.transaction():
                payload_hash = hashlib.sha256(json.dumps([profile, risk, plan, kind, details],
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if execution:
                    row = await (await conn.execute("SELECT * FROM hp_tasks WHERE id = %s FOR UPDATE", (execution.task_id,))).fetchone()
                    if (not row or user_id != "account:" + execution.user_id or row["user_id"] != execution.user_id
                            or row["status"] != "running" or row["worker_token"] != execution.worker_token
                            or row["cancel_requested"] or row["lease_until"] < int(time.time())):
                        raise RuntimeError("任务已失去写入资格，健康记录未保存。")
                    prior = await (await conn.execute("SELECT payload_hash FROM hp_task_effects WHERE task_id = %s AND kind = %s",
                                                     (execution.task_id, kind))).fetchone()
                    if prior:
                        if prior["payload_hash"] != payload_hash:
                            raise RuntimeError("同一任务的健康写入参数发生变化，已拒绝覆盖。")
                        return
                await _write_assessment(AsyncPostgresStore(conn), user_id, profile, risk, plan, kind, details, execution)
                if execution:
                    await conn.execute("INSERT INTO hp_task_effects VALUES (%s, %s, %s, %s)",
                                       (execution.task_id, kind, payload_hash, time.time_ns()))
        return
    await _write_assessment(store, user_id, profile, risk, plan, kind, details, execution)


async def _write_assessment(store, user_id, profile, risk, plan, kind, details, execution):
    _, records = await load_memory(store, user_id)
    record = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **details,
              "risk_percent": risk["risk_percent"], "level": risk["level"],
              **{key: profile.get(key) for key in ("sbp", "dbp", "total_chol", "hdl", "smoker")}}
    if execution:
        record["task_id"] = execution.task_id
    namespace = ("users", user_id)
    await store.abatch([PutOp(namespace, "profile", dict(profile)),
                       PutOp(namespace, "history", {"records": [*records, record]}),
                       PutOp(namespace, "followup", plan)])
