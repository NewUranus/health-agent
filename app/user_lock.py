"""跨进程串行化同一账号的健康图执行，连接断开时由 PostgreSQL 释放锁。"""
from contextlib import asynccontextmanager
import hashlib
import os


@asynccontextmanager
async def user_execution_lock(user_id):
    url = os.getenv("DATABASE_URL")
    if url and user_id.startswith("account:"):
        from psycopg import AsyncConnection
        key = int.from_bytes(hashlib.sha256(("healthpilot:" + user_id).encode()).digest()[:8], signed=True)
        async with await AsyncConnection.connect(url, autocommit=True, connect_timeout=5) as connection:
            await connection.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                await connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
    else:
        # 无数据库模式的进程内锁由 HealthAgent.stream 维护，不支持多进程健康记忆。
        yield
