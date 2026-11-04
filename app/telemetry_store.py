"""集中日志存储与有界本地持久缓冲，不依赖业务事务。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from app.database import schema_lock_sync

ROOT = Path(__file__).resolve().parent.parent
COLUMNS = "id, at, level, service, instance, trace_id, span_id, parent_span_id, request_id, task_id, event, message, attributes, search_text"


class LogStore:
    def __init__(self):
        self.url = os.getenv("HEALTH_AGENT_LOG_DATABASE_URL") or os.getenv("DATABASE_URL", "")
        self.path = Path(os.getenv("HEALTH_AGENT_LOG_DB", str(ROOT / "runs" / "logs" / "central.sqlite3")))

    @contextmanager
    def connection(self):
        if self.url:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=2, options="-c statement_timeout=5000")
        else:
            conn = sqlite3.connect(self.path, timeout=2)
            conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield lambda query, args=(): conn.execute(query.replace("?", "%s") if self.url else query, args)
        finally:
            conn.close()

    def setup(self):
        if not self.url:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            self.path.chmod(0o600)
        with schema_lock_sync(self.url), self.connection() as execute:
            execute("""CREATE TABLE IF NOT EXISTS hp_logs (
                id TEXT PRIMARY KEY, at BIGINT NOT NULL, level TEXT NOT NULL, service TEXT NOT NULL,
                instance TEXT NOT NULL, trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT NOT NULL,
                request_id TEXT NOT NULL, task_id TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL,
                attributes TEXT NOT NULL, search_text TEXT NOT NULL)""")
            for field in ("trace_id", "request_id", "task_id", "service", "level", "event"):
                execute(f"CREATE INDEX IF NOT EXISTS hp_logs_{field} ON hp_logs({field}, at, id)")
            execute("CREATE INDEX IF NOT EXISTS hp_logs_time ON hp_logs(at, id)")
            if self.url:
                execute("CREATE INDEX IF NOT EXISTS hp_logs_search ON hp_logs USING GIN (to_tsvector('simple', search_text))")

    def insert(self, records):
        with self.connection() as execute:
            for record in records:
                attributes = json.dumps(record["attributes"], ensure_ascii=False)
                values = [record.get(key, "") for key in COLUMNS.split(", ")[:-2]] + [attributes, record["event"] + " " + record["message"] + " " + attributes]
                execute(f"INSERT INTO hp_logs ({COLUMNS}) VALUES ({', '.join('?' for _ in values)}) ON CONFLICT (id) DO NOTHING", values)

    def prune(self, days):
        cutoff = int(time.time() * 1000) - days * 86400000
        with self.connection() as execute:
            execute("DELETE FROM hp_logs WHERE id IN (SELECT id FROM hp_logs WHERE at < ? ORDER BY at LIMIT 5000)", (cutoff,))

    def search(self, *, start, end, limit=100, before=None, q="", **filters):
        clauses, args = ["at >= ?", "at <= ?"], [start, end]
        for key in ("trace_id", "request_id", "task_id", "service", "level", "event"):
            if filters.get(key):
                clauses.append(f"{key} = ?")
                args.append(filters[key])
        if before:
            cursor_at, cursor_id = before.split(":", 1)
            clauses.append("(at < ? OR (at = ? AND id < ?))")
            args.extend([int(cursor_at), int(cursor_at), cursor_id])
        if q:
            if self.url:
                clauses.append("to_tsvector('simple', search_text) @@ plainto_tsquery('simple', ?)")
                args.append(q)
            else:
                clauses.append("search_text LIKE ? ESCAPE '\\'")
                args.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        with self.connection() as execute:
            rows = execute("SELECT " + COLUMNS + " FROM hp_logs WHERE " + " AND ".join(clauses) + " ORDER BY at DESC, id DESC LIMIT ?", (*args, limit + 1)).fetchall()
        items = []
        for row in rows[:limit]:
            item = {key: row[key] for key in row.keys() if key != "search_text"}
            item["attributes"] = json.loads(item["attributes"])
            items.append(item)
        cursor = f'{items[-1]["at"]}:{items[-1]["id"]}' if len(rows) > limit else None
        return {"records": items, "next_cursor": cursor}


class LogBuffer:
    """各进程共享 SQLite WAL 缓冲；中心写入按事件 ID 去重。"""
    def __init__(self):
        self.path = Path(os.getenv("HEALTH_AGENT_LOG_SPOOL", str(ROOT / "runs" / "logs" / "spool.sqlite3")))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        self.max_rows = int(os.getenv("HEALTH_AGENT_LOG_BUFFER_ROWS", "100000"))
        self.days = int(os.getenv("HEALTH_AGENT_LOG_RETENTION_DAYS", "30"))
        if not 1000 <= self.max_rows <= 1000000 or not 1 <= self.days <= 3650:
            raise ValueError("日志缓冲条数或保留天数超出范围。")
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS pending (seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.last_error = None
        self.last_success = None
        self.local_dropped = 0
        self.store = LogStore()
        self.thread = threading.Thread(target=self.work, name="healthpilot-log-export", daemon=True)
        self.thread.start()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=0.2)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def append(self, record):
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                size = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
                if size >= self.max_rows:
                    count = min(1000, size)
                    conn.execute("DELETE FROM pending WHERE seq IN (SELECT seq FROM pending ORDER BY seq LIMIT ?)", (count,))
                    conn.execute("INSERT INTO counters VALUES ('dropped', ?) ON CONFLICT(name) DO UPDATE SET value = value + excluded.value", (count,))
                    self.diagnostic("telemetry.buffer_full", count)
                conn.execute("INSERT INTO pending(payload) VALUES (?)", (json.dumps(record, ensure_ascii=False),))
            self.wake.set()
        except Exception:
            self.local_dropped += 1
            self.diagnostic("telemetry.spool_unavailable", 1)

    @staticmethod
    def diagnostic(event, dropped):
        # 不再次调用 logging，防止故障日志递归；不输出异常正文。
        try:
            os.write(2, (json.dumps({"at": int(time.time() * 1000), "level": "ERROR", "service": "telemetry", "event": event, "dropped": dropped}) + "\n").encode())
        except OSError:
            pass

    def work(self):
        ready, retry, prune_at = False, 1, 0
        while not self.stop.is_set():
            try:
                if not ready:
                    self.store.setup()
                    ready = True
                with self.connect() as conn:
                    rows = conn.execute("SELECT seq, payload FROM pending ORDER BY seq LIMIT 250").fetchall()
                if rows:
                    self.store.insert([json.loads(row[1]) for row in rows])
                    with self.connect() as conn:
                        conn.executemany("DELETE FROM pending WHERE seq = ?", [(row[0],) for row in rows])
                    self.last_success = int(time.time() * 1000)
                if time.time() >= prune_at:
                    self.store.prune(self.days)
                    prune_at = time.time() + 60
                self.last_error, retry = None, 1
                if len(rows) < 250:
                    self.wake.wait(1)
                    self.wake.clear()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.stop.wait(retry)
                retry = min(30, retry * 2)

    def status(self):
        try:
            with self.connect() as conn:
                pending = conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]
                dropped = conn.execute("SELECT value FROM counters WHERE name = 'dropped'").fetchone()
            return {"pending": pending, "dropped": (dropped[0] if dropped else 0) + self.local_dropped,
                "last_success": self.last_success, "export_error": self.last_error, "retention_days": self.days,
                "central_backend": "postgresql" if self.store.url else "sqlite"}
        except Exception:
            return {"pending": None, "dropped": self.local_dropped, "export_error": "spool_unavailable"}

    def close(self):
        self.stop.set()
        self.wake.set()
        self.thread.join(timeout=2)
