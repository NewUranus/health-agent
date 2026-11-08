"""持久化对话任务、幂等提交和可回放事件；不自动重放结果不明的任务。"""
import asyncio
from contextlib import aclosing, suppress
import json
import logging
import time
import uuid

from app import guests
from app.auth import GUEST_ROLE, AuthFailure, digest
from app.http_security import request_id_context
from app.telemetry import bind, carrier, extract, span, traced, emit, opaque_id
from app.task_context import TaskExecution, task_execution
from app.database import schema_lock_sync
from app.medical_review import enqueue_review

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "waiting_human", "failed", "cancelled", "timed_out", "interrupted"}
FAILURES = {"failed", "cancelled", "timed_out", "interrupted"}
LEASE_SECONDS = 30


class TaskStore:
    def __init__(self, database):
        self.db = database
        from app.runtime_config import settings
        config = settings()
        self.queue_limit = config["HEALTH_AGENT_QUEUE_LIMIT"]
        self.user_queue_limit = config["HEALTH_AGENT_USER_QUEUE_LIMIT"]

    def setup(self):
        with schema_lock_sync(self.db.url), self.db.connection() as execute:
            for sql in (
                "CREATE TABLE IF NOT EXISTS hp_tasks (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, thread_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, request_id TEXT NOT NULL, worker_token TEXT, lease_until BIGINT, cancel_requested INTEGER NOT NULL DEFAULT 0, error_code TEXT, UNIQUE(user_id, idempotency_key))",
                "CREATE INDEX IF NOT EXISTS hp_tasks_queue ON hp_tasks(status, created_at)",
                "CREATE INDEX IF NOT EXISTS hp_tasks_user ON hp_tasks(user_id, created_at)",
                "CREATE INDEX IF NOT EXISTS hp_tasks_conversation ON hp_tasks(user_id, thread_id, created_at)",
                "CREATE TABLE IF NOT EXISTS hp_task_traces (task_id TEXT PRIMARY KEY REFERENCES hp_tasks(id), context TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_task_events (task_id TEXT NOT NULL REFERENCES hp_tasks(id), seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id, seq))",
                "CREATE TABLE IF NOT EXISTS hp_task_slots (user_id TEXT PRIMARY KEY REFERENCES hp_users(id), task_id TEXT NOT NULL REFERENCES hp_tasks(id))",
                "CREATE TABLE IF NOT EXISTS hp_task_effects (task_id TEXT NOT NULL REFERENCES hp_tasks(id), kind TEXT NOT NULL, payload_hash TEXT NOT NULL, committed_at BIGINT NOT NULL, PRIMARY KEY(task_id, kind))",
                "CREATE TABLE IF NOT EXISTS hp_task_checkpoints (task_id TEXT PRIMARY KEY REFERENCES hp_tasks(id), thread_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, committed_at BIGINT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_write_idempotency (user_id TEXT NOT NULL, scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, result TEXT, created_at BIGINT NOT NULL, PRIMARY KEY (user_id, scope, idempotency_key))",
                "CREATE TABLE IF NOT EXISTS hp_conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), thread_id TEXT NOT NULL, title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL, UNIQUE(user_id, thread_id))",
                "CREATE INDEX IF NOT EXISTS hp_conversations_user ON hp_conversations(user_id, archived, updated_at)",
                "CREATE TABLE IF NOT EXISTS hp_queue_gate (id INTEGER PRIMARY KEY)",
                "INSERT INTO hp_queue_gate VALUES (1) ON CONFLICT (id) DO NOTHING",
                "CREATE TABLE IF NOT EXISTS hp_system_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "INSERT INTO hp_system_settings VALUES ('admission_paused', '0') ON CONFLICT (name) DO NOTHING",
                "CREATE TABLE IF NOT EXISTS hp_task_priorities (task_id TEXT PRIMARY KEY REFERENCES hp_tasks(id), priority INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_workers (id TEXT PRIMARY KEY, slots INTEGER NOT NULL, last_seen BIGINT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_config_snapshots (id TEXT PRIMARY KEY, at BIGINT NOT NULL, payload TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_task_reviews (task_id TEXT PRIMARY KEY REFERENCES hp_tasks(id), disposition TEXT NOT NULL, reviewed_at BIGINT NOT NULL, request_id TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS hp_medical_reviews (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES hp_tasks(id), user_id TEXT NOT NULL REFERENCES hp_users(id), thread_id TEXT NOT NULL, question TEXT NOT NULL, reply TEXT NOT NULL, risk_level TEXT NOT NULL, risk_percent REAL, reasons TEXT NOT NULL, status TEXT NOT NULL, reviewer_id TEXT, doctor_note TEXT NOT NULL, created_at BIGINT NOT NULL, reviewed_at BIGINT)",
                "CREATE INDEX IF NOT EXISTS hp_medical_reviews_status ON hp_medical_reviews(status, created_at)",
                "CREATE INDEX IF NOT EXISTS hp_medical_reviews_user ON hp_medical_reviews(user_id, created_at)",
            ):
                execute(sql)
            rows = execute("SELECT user_id, thread_id, MAX(created_at) AS updated_at FROM hp_tasks t WHERE NOT EXISTS (SELECT 1 FROM hp_conversations c WHERE c.user_id = t.user_id AND c.thread_id = t.thread_id) GROUP BY user_id, thread_id").fetchall()
            for row in rows:
                execute("INSERT INTO hp_conversations VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT (user_id, thread_id) DO NOTHING",
                        (uuid.uuid4().hex, row["user_id"], row["thread_id"], "健康会话", row["updated_at"]))

    @staticmethod
    def public(row):
        return {**{key: row[key] for key in ("id", "thread_id", "status", "request_id", "error_code")},
                "created_at": row["created_at"] // 1_000_000,
                "updated_at": row["updated_at"] // 1_000_000,
                "cancel_requested": bool(row["cancel_requested"]),
                "health_committed": bool(row["health_committed"]) if "health_committed" in row.keys() and row["health_committed"] is not None else None,
                "checkpoint_bound": bool(row["checkpoint_bound"]) if "checkpoint_bound" in row.keys() and row["checkpoint_bound"] is not None else None,
                "failure_category": {"failed": "execution", "cancelled": "cancelled", "timed_out": "timeout", "interrupted": "infrastructure"}.get(row["status"]),
                "review_disposition": row["review_disposition"] if "review_disposition" in row.keys() else None}

    @property
    def select_public(self):
        # SQLite 任务与内存健康记录不在同一事务内，不能声称已确认持久化。
        committed = "EXISTS (SELECT 1 FROM hp_task_effects e WHERE e.task_id = hp_tasks.id)" if self.db.url else "NULL"
        checkpoint = "EXISTS (SELECT 1 FROM hp_task_checkpoints c WHERE c.task_id = hp_tasks.id)" if self.db.url else "NULL"
        return f"SELECT hp_tasks.*, {committed} AS health_committed, {checkpoint} AS checkpoint_bound, (SELECT disposition FROM hp_task_reviews r WHERE r.task_id = hp_tasks.id) AS review_disposition FROM hp_tasks"

    @staticmethod
    def _event(execute, task_id, event):
        previous = execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM hp_task_events WHERE task_id = ?", (task_id,)).fetchone()["seq"]
        execute("INSERT INTO hp_task_events VALUES (?, ?, ?)",
                (task_id, previous + 1, json.dumps(event, ensure_ascii=False)))

    @traced("queue.submit", kind="producer")
    def submit(self, user_id, key, payload, request_id):
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        payload_hash = digest(serialized)
        task_id, now = uuid.uuid4().hex, time.time_ns()
        with self.db.connection() as execute:
            execute("SELECT id FROM hp_queue_gate WHERE id = 1" + (" FOR UPDATE" if self.db.url else "")).fetchone()
            existing = execute(self.select_public + " WHERE user_id = ? AND idempotency_key = ?", (user_id, key)).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise AuthFailure(409, "IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求，请勿修改后重用。")
                return self.public(existing), False
            if execute("SELECT value FROM hp_system_settings WHERE name = 'admission_paused'").fetchone()["value"] == "1":
                raise AuthFailure(503, "ADMISSION_PAUSED", "服务暂时停止接收新任务，请稍后重试。已有任务继续执行。")
            owner = execute("SELECT role FROM hp_users WHERE id = ?", (user_id,)).fetchone()
            if owner and owner["role"] == GUEST_ROLE:
                if payload.get("web_search"):
                    raise guests.login_required("联网搜索")
                guests.consume_message(execute, user_id)
            counts = execute("SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END), 0) AS owned FROM hp_tasks WHERE status IN ('pending', 'running')", (user_id,)).fetchone()
            if counts["total"] >= self.queue_limit or counts["owned"] >= self.user_queue_limit:
                raise AuthFailure(429, "QUEUE_CAPACITY", "任务队列已达到容量限制，请等待已有任务完成后再提交。")
            execute("INSERT INTO hp_tasks (id, user_id, idempotency_key, payload_hash, thread_id, payload, status, created_at, updated_at, request_id) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?) ON CONFLICT (user_id, idempotency_key) DO NOTHING",
                    (task_id, user_id, key, payload_hash, payload["thread_id"], serialized, now, now, request_id))
            row = execute(self.select_public + " WHERE user_id = ? AND idempotency_key = ?", (user_id, key)).fetchone()
            if row["payload_hash"] != payload_hash:
                raise AuthFailure(409, "IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求，请勿修改后重用。")
            created = row["id"] == task_id
            if created:
                execute("INSERT INTO hp_task_traces VALUES (?, ?)", (task_id, json.dumps({**carrier(), "task_id": task_id, "request_id": request_id, "thread_id": opaque_id(payload["thread_id"], "thread"), "user_hash": opaque_id(user_id)})))
                suffix = " FOR UPDATE" if self.db.url else ""
                conversation = execute("SELECT archived FROM hp_conversations WHERE user_id = ? AND thread_id = ?" + suffix, (user_id, payload["thread_id"])).fetchone()
                if conversation and conversation["archived"]:
                    raise AuthFailure(409, "CONVERSATION_ARCHIVED", "此会话已归档，请先在工作台恢复。")
                execute("INSERT INTO hp_conversations VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT (user_id, thread_id) DO UPDATE SET updated_at = excluded.updated_at",
                        (uuid.uuid4().hex, user_id, payload["thread_id"], "健康会话", now))
                self.db._audit(execute, user_id, "task.submitted", task_id, request_id, 202)
            return self.public(row), created

    def trace_context(self, task_id):
        with self.db.connection() as execute:
            row = execute("SELECT context FROM hp_task_traces WHERE task_id = ?", (task_id,)).fetchone()
        return extract(json.loads(row["context"])) if row else {}

    def get(self, task_id, user_id):
        with self.db.connection() as execute:
            row = execute(self.select_public + " WHERE id = ? AND user_id = ?", (task_id, user_id)).fetchone()
        if not row:
            raise AuthFailure(404, "TASK_NOT_FOUND", "任务不存在或无权访问。")
        return self.public(row)

    def list(self, user_id, limit=30, failed=False):
        with self.db.connection() as execute:
            condition = " AND status IN ('failed', 'cancelled', 'timed_out', 'interrupted')" if failed else ""
            rows = execute(self.select_public + " WHERE user_id = ?" + condition + " ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
        return [self.public(row) for row in rows]

    def review(self, task_id, user_id, disposition, request_id):
        if disposition not in {"resolved", "needs_support"}:
            raise AuthFailure(422, "INVALID_REVIEW", "请选择已核对或需要人工协助。")
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if not row or row["user_id"] != user_id:
                raise AuthFailure(404, "TASK_NOT_FOUND", "任务不存在或无权访问。")
            if row["status"] not in FAILURES:
                raise AuthFailure(409, "TASK_NOT_FAILED", "只能记录失败、超时、中断或取消任务的处理结果。")
            previous = execute("SELECT disposition FROM hp_task_reviews WHERE task_id = ?", (task_id,)).fetchone()
            if not previous or previous["disposition"] != disposition:
                execute("INSERT INTO hp_task_reviews VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET disposition = excluded.disposition, reviewed_at = excluded.reviewed_at, request_id = excluded.request_id",
                    (task_id, disposition, time.time_ns(), request_id))
                self.db._audit(execute, user_id, "task.review." + disposition, task_id, request_id, 200)
        return self.get(task_id, user_id)

    def events(self, task_id, user_id, after=0):
        task = self.get(task_id, user_id)
        with self.db.connection() as execute:
            rows = execute("SELECT seq, payload FROM hp_task_events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT 200", (task_id, after)).fetchall()
        # 先读状态再读事件：终态事务包含最后事件，因此终态不会遗漏最后一页。
        return task, [{"seq": row["seq"], "event": json.loads(row["payload"])} for row in rows]

    def _lock_task(self, execute, task_id):
        suffix = " FOR UPDATE" if self.db.url else ""
        return execute("SELECT * FROM hp_tasks WHERE id = ?" + suffix, (task_id,)).fetchone()

    def _finish(self, execute, row, status, code=None, message=None):
        if status not in TERMINAL or row["status"] not in {"pending", "running"}:
            raise ValueError("非法任务状态流转。")
        if message:
            self._event(execute, row["id"], {"type": "error", "message": message, "code": code,
                "request_id": row["request_id"], "retryable": False, "next_action": "先检查已有评估和随访记录，再决定是否新建任务。"})
        self._event(execute, row["id"], {"type": "done"})
        execute("UPDATE hp_tasks SET status = ?, error_code = ?, updated_at = ?, lease_until = NULL WHERE id = ?",
                (status, code, time.time_ns(), row["id"]))
        execute("DELETE FROM hp_task_slots WHERE task_id = ?", (row["id"],))
        self.db._audit(execute, row["user_id"], "task." + status, row["id"], row["request_id"],
                       200 if status in {"succeeded", "waiting_human"} else 409)
        if status in {"succeeded", "waiting_human"}:
            enqueue_review(execute, row)

    def cancel(self, task_id, user_id, request_id):
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if not row or row["user_id"] != user_id:
                raise AuthFailure(404, "TASK_NOT_FOUND", "任务不存在或无权访问。")
            if row["status"] == "pending":
                self._finish(execute, row, "cancelled", "TASK_CANCELLED", "任务已取消。")
            elif row["status"] == "running":
                execute("UPDATE hp_tasks SET cancel_requested = 1, updated_at = ? WHERE id = ?", (time.time_ns(), task_id))
            self.db._audit(execute, user_id, "task.cancel_requested", task_id, request_id, 200)
        return self.get(task_id, user_id)

    def recover_stale(self):
        """只关闭失去租约的运行任务，保留证据；未开始的任务仍可领取。"""
        with self.db.connection() as execute:
            suffix = " FOR UPDATE SKIP LOCKED" if self.db.url else ""
            rows = execute("SELECT * FROM hp_tasks WHERE status = 'running' AND lease_until < ?" + suffix,
                           (int(time.time()),)).fetchall()
            for row in rows:
                self._finish(execute, row, "interrupted", "WORKER_LOST", "执行进程中断，结果可能已部分写入，请核对记录；不会自动重复执行。")

    def claim(self, worker_token):
        with self.db.connection() as execute:
            suffix = " FOR UPDATE SKIP LOCKED" if self.db.url else ""
            row = execute("SELECT * FROM hp_tasks WHERE status = 'pending' AND user_id NOT IN (SELECT user_id FROM hp_task_slots) ORDER BY COALESCE((SELECT priority FROM hp_task_priorities p WHERE p.task_id = hp_tasks.id), 0) DESC, created_at LIMIT 1" + suffix).fetchone()
            if not row:
                return None
            acquired = execute("INSERT INTO hp_task_slots VALUES (?, ?) ON CONFLICT (user_id) DO NOTHING",
                               (row["user_id"], row["id"])).rowcount
            if not acquired:
                return None
            execute("UPDATE hp_tasks SET status = 'running', worker_token = ?, lease_until = ?, updated_at = ? WHERE id = ?",
                    (worker_token, int(time.time()) + LEASE_SECONDS, time.time_ns(), row["id"]))
            self.db._audit(execute, row["user_id"], "task.running", row["id"], row["request_id"], 200)
            return {**dict(row), "worker_token": worker_token}

    def heartbeat(self, task_id, token):
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if not row or row["status"] != "running" or row["worker_token"] != token:
                return False
            execute("UPDATE hp_tasks SET lease_until = ? WHERE id = ?", (int(time.time()) + LEASE_SECONDS, task_id))
            return not row["cancel_requested"]

    def append(self, task_id, token, event):
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if not row or row["status"] != "running" or row["worker_token"] != token or row["cancel_requested"]:
                return False
            self._event(execute, task_id, event)
            return True

    def reserve_write(self, user_id, scope, key, payload_hash):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise AuthFailure(422, "INVALID_IDEMPOTENCY_KEY", "幂等键须为 8–128 位字母、数字或 . _ : -。")
        with self.db.connection() as execute:
            suffix = " FOR UPDATE" if self.db.url else ""
            row = execute("SELECT payload_hash, result FROM hp_write_idempotency WHERE user_id = ? AND scope = ? AND idempotency_key = ?" + suffix,
                          (user_id, scope, key)).fetchone()
            if row:
                if row["payload_hash"] != payload_hash:
                    raise AuthFailure(409, "IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求，请勿修改后重用。")
                if row["result"]:
                    return json.loads(row["result"])
                raise AuthFailure(409, "IDEMPOTENCY_IN_PROGRESS", "相同幂等键的请求正在处理，请稍后重试。")
            execute("INSERT INTO hp_write_idempotency VALUES (?, ?, ?, ?, NULL, ?)",
                    (user_id, scope, key, payload_hash, time.time_ns()))
        return None

    def complete_write(self, user_id, scope, key, result):
        with self.db.connection() as execute:
            execute("UPDATE hp_write_idempotency SET result = ? WHERE user_id = ? AND scope = ? AND idempotency_key = ?",
                    (json.dumps(result, ensure_ascii=False), user_id, scope, key))

    def release_write(self, user_id, scope, key):
        with self.db.connection() as execute:
            execute("DELETE FROM hp_write_idempotency WHERE user_id = ? AND scope = ? AND idempotency_key = ? AND result IS NULL",
                    (user_id, scope, key))

    def _bind_checkpoint(self, execute, task_id, graph_thread):
        """与任务终态同一事务记录当前检查点；没有 PostgreSQL 检查点表时跳过。"""
        if not self.db.url or not graph_thread:
            return
        relation = execute("SELECT to_regclass('public.checkpoints') AS name").fetchone()
        if not relation or not relation["name"]:
            return
        latest = execute("SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = '' ORDER BY checkpoint->>'ts' DESC LIMIT 1",
                         (graph_thread,)).fetchone()
        if not latest:
            return
        execute("INSERT INTO hp_task_checkpoints VALUES (?, ?, ?, ?) ON CONFLICT (task_id) DO UPDATE SET thread_id = excluded.thread_id, checkpoint_id = excluded.checkpoint_id, committed_at = excluded.committed_at",
                (task_id, graph_thread, latest["checkpoint_id"], time.time_ns()))

    def finish(self, task_id, token, status, code=None, message=None, graph_thread=None):
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if row and row["status"] == "running" and row["worker_token"] == token:
                if row["cancel_requested"]:
                    status, code, message = "cancelled", "TASK_CANCELLED", "任务已停止，已产生的记录不会回滚。"
                self._finish(execute, row, status, code, message)
                self._bind_checkpoint(execute, row["id"], graph_thread)


class TaskRunner:
    """有界的数据库队列消费者，可嵌入单机 API 或在独立进程运行。"""
    def __init__(self, store, agent, *, workers=2, timeout=300):
        self.store, self.agent = store, agent
        self.workers, self.timeout = workers, timeout
        self.loops = []
        self.worker_id = uuid.uuid4().hex
        self.heartbeat_task = None

    def heartbeat_worker(self, remove=False):
        with self.store.db.connection() as execute:
            if remove:
                execute("DELETE FROM hp_workers WHERE id = ?", (self.worker_id,))
            else:
                execute("INSERT INTO hp_workers VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen = excluded.last_seen, slots = excluded.slots", (self.worker_id, self.workers, int(time.time())))
                execute("DELETE FROM hp_workers WHERE last_seen < ?", (int(time.time()) - 86400,))

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(self.heartbeat_worker)
            except Exception as exc:
                emit("worker.heartbeat.failed", level="ERROR", error_type=type(exc).__name__)

    async def start(self):
        await asyncio.to_thread(self.heartbeat_worker)
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        await asyncio.to_thread(self.store.recover_stale)
        self.loops = [asyncio.create_task(self.loop()) for _ in range(self.workers)]

    async def close(self):
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            await asyncio.gather(self.heartbeat_task, return_exceptions=True)
        for loop in self.loops:
            loop.cancel()
        await asyncio.gather(*self.loops, return_exceptions=True)
        await asyncio.to_thread(self.heartbeat_worker, True)

    async def loop(self):
        while True:
            try:
                await asyncio.to_thread(self.store.recover_stale)
                job = await asyncio.to_thread(self.store.claim, uuid.uuid4().hex)
                if job:
                    await self.run(job)
                else:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                emit("worker.consumer.failed", level="ERROR", error_type=type(exc).__name__)
                await asyncio.sleep(1)

    async def run(self, job):
        parent = await asyncio.to_thread(self.store.trace_context, job["id"])
        parent.update(task_id=job["id"], request_id=job["request_id"],
                      thread_id=opaque_id(job["thread_id"], "thread"), user_hash=opaque_id(job["user_id"]))
        with bind(**parent), span("worker.execute", kind="consumer",
                queue_wait_ms=max(0, (time.time_ns() - job["created_at"]) / 1000000)) as fields:
            await self._run(job)
            result = await asyncio.to_thread(self.store.get, job["id"], job["user_id"])
            fields["status"] = "error" if result["status"] in {"failed", "timed_out", "interrupted"} else result["status"]
            fields["code"] = result.get("error_code")

    async def _run(self, job):
        task_id, token = job["id"], job["worker_token"]
        context = request_id_context.set(job["request_id"])
        execution_context = task_execution.set(TaskExecution(task_id, token, job["user_id"]))
        graph_thread = {"value": None}

        def settle(status, code=None, message=None):
            self.store.finish(task_id, token, status, code, message, graph_thread["value"])

        async def consume():
            payload = json.loads(job["payload"])
            thread = await asyncio.to_thread(self.store.db.claim_thread, payload["thread_id"], job["user_id"])
            graph_thread["value"] = thread
            failed, waiting, complete, message_seen = False, False, False, False
            async with aclosing(self.agent.stream(thread, payload["message"], "account:" + job["user_id"],
                                                  web_search=payload.get("web_search", False))) as stream:
                async for event in stream:
                    if event["type"] == "done":
                        complete = True
                        continue
                    waiting |= event.get("node") == "checkin_question"
                    failed |= event["type"] == "error"
                    message_seen |= event["type"] == "message"
                    if event["type"] == "error":
                        event = {**event, "code": "AGENT_EXECUTION_FAILED", "request_id": job["request_id"], "retryable": False}
                    if not await asyncio.to_thread(self.store.append, task_id, token, event):
                        raise asyncio.CancelledError()
            status = "failed" if failed or not complete or not message_seen else "waiting_human" if waiting else "succeeded"
            await asyncio.to_thread(settle, status,
                                    "AGENT_EXECUTION_FAILED" if status == "failed" else None,
                                    "任务未能完整返回，请检查已有记录。" if not complete or not message_seen else None)

        async def watch():
            while True:
                await asyncio.sleep(0.5)
                if not await asyncio.to_thread(self.store.heartbeat, task_id, token):
                    return

        execution, watcher = asyncio.create_task(consume()), asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({execution, watcher}, timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED)
            if execution in done and not execution.cancelled():
                await execution
                return
            if watcher in done:
                await watcher
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            status, code, message = ("cancelled", "TASK_CANCELLED", "任务已停止，已产生的记录不会回滚。") if done else ("timed_out", "TASK_TIMEOUT", "任务超时，请先核对已有记录。")
            await asyncio.to_thread(settle, status, code, message)
        except asyncio.CancelledError:
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            await asyncio.to_thread(settle, "interrupted", "WORKER_STOPPED", "执行服务已停止，请核对已有记录。")
            raise
        except Exception as exc:
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution
            emit("task.execution.failed", level="ERROR", error_type=type(exc).__name__)
            await asyncio.to_thread(settle, "failed", "TASK_EXECUTION_FAILED", "任务执行失败，请核对已有记录。")
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await watcher
            request_id_context.reset(context)
            task_execution.reset(execution_context)
