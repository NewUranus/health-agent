"""跨进程复用确定性计算结果；只保存工具结果和查询向量，不保存检索命中。"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from app.database import schema_lock_sync

ROOT = Path(__file__).resolve().parent.parent
TOOL_REVISION = "health-tools-v1"
KINDS = ("tool", "vector")
ROW_LIMIT = 2000
WEB_TOOLS = frozenset({"search_web", "read_web_document"})


def tool_key(name, args):
    raw = json.dumps({"revision": TOOL_REVISION, "tool": name, "args": args}, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def embedder_model(embedder):
    owner = getattr(embedder, "__self__", None)
    return getattr(owner, "model", None) or os.getenv("QWEN_EMBEDDING_MODEL") or "default"


def vector_key(model, query):
    raw = json.dumps({"model": model, "query": query}, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class ResultCache:
    """与账号库共用 SQLite 或 PostgreSQL，供 API 和 Worker 读取同一份结果。"""

    def __init__(self):
        self.url = os.getenv("DATABASE_URL", "")
        self.path = Path(os.getenv("HEALTH_AGENT_AUTH_DB", str(ROOT / "runs" / "auth.sqlite3")))

    def connection(self):
        return _connection(self.url, self.path)

    def setup(self):
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
        with schema_lock_sync(self.url), self.connection() as execute:
            ensure_schema(execute)

    def read(self, kind, key):
        with self.connection() as execute:
            row = execute("SELECT payload FROM hp_result_cache WHERE cache_key = ? AND kind = ?", (key, kind)).fetchone()
        if not row:
            return None
        return json.loads(row["payload"])

    def write(self, kind, key, payload):
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.connection() as execute:
            execute("INSERT INTO hp_result_cache VALUES (?, ?, ?, ?) ON CONFLICT (cache_key) DO NOTHING",
                    (key, kind, encoded, time.time_ns()))
            count = execute("SELECT COUNT(*) AS count FROM hp_result_cache WHERE kind = ?", (kind,)).fetchone()["count"]
            if count > ROW_LIMIT:
                execute("DELETE FROM hp_result_cache WHERE kind = ? AND cache_key NOT IN (SELECT cache_key FROM hp_result_cache WHERE kind = ? ORDER BY created_at DESC LIMIT ?)",
                        (kind, kind, ROW_LIMIT))

    def record(self, kind, hit):
        column = "hits" if hit else "misses"
        with self.connection() as execute:
            execute(f"UPDATE hp_cache_counters SET {column} = {column} + 1 WHERE kind = ?", (kind,))

    def stats(self):
        with self.connection() as execute:
            return cache_snapshot(execute)


def cache_snapshot(execute):
    rows = execute("SELECT kind, hits, misses FROM hp_cache_counters").fetchall()
    found = {row["kind"]: {"hits": row["hits"], "misses": row["misses"], "saved": row["hits"]} for row in rows}
    return {kind: found.get(kind, {"hits": 0, "misses": 0, "saved": 0}) for kind in KINDS}


def ensure_schema(execute):
    execute("CREATE TABLE IF NOT EXISTS hp_result_cache (cache_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at BIGINT NOT NULL)")
    execute("CREATE INDEX IF NOT EXISTS hp_result_cache_kind ON hp_result_cache(kind, created_at)")
    execute("CREATE TABLE IF NOT EXISTS hp_cache_counters (kind TEXT PRIMARY KEY, hits BIGINT NOT NULL, misses BIGINT NOT NULL)")
    for kind in KINDS:
        execute("INSERT INTO hp_cache_counters VALUES (?, 0, 0) ON CONFLICT (kind) DO NOTHING", (kind,))


def _connection(url, path):
    if url:
        import psycopg
        from psycopg.rows import dict_row
        conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=5)
    else:
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

    class _Context:
        def __enter__(self):
            return lambda sql, args=(): conn.execute(sql.replace("?", "%s") if url else sql, args)

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    conn.commit()
                else:
                    conn.rollback()
            finally:
                conn.close()
            return False

    return _Context()
