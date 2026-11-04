"""旧版日志只读查询适配；新日志通过 OpenTelemetry 导出到 Loki。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3


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
