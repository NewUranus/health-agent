"""协调同一 PostgreSQL 数据库的首次建表，避免多进程迁移竞争。"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import time


SCHEMA_LOCK = 72610420261104


@asynccontextmanager
async def schema_lock(url):
    from psycopg import AsyncConnection
    # LangGraph 迁移使用自己的连接，故在独立连接上持有会话锁。
    async with await AsyncConnection.connect(url, autocommit=True, connect_timeout=5) as conn:
        # 不在阻塞 SQL 内等锁：其活动快照会阻碍迁移中的并发建索引。
        async with asyncio.timeout(60):
            while not (await (await conn.execute("SELECT pg_try_advisory_lock(%s)", (SCHEMA_LOCK,))).fetchone())[0]:
                await asyncio.sleep(0.1)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK,))


@contextmanager
def schema_lock_sync(url):
    if not url:
        yield
        return
    import psycopg
    with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
        deadline = time.monotonic() + 60
        while not conn.execute("SELECT pg_try_advisory_lock(%s)", (SCHEMA_LOCK,)).fetchone()[0]:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待数据库初始化锁超时。")
            time.sleep(0.1)
        try:
            yield
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK,))
