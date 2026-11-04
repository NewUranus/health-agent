"""持久化对话任务、幂等提交和可回放事件；不自动重放结果不明的任务。"""
import asyncio
from contextlib import aclosing, suppress
import json
import logging
import time
import uuid

from app.auth import AuthFailure, digest
from app.http_security import request_id_context
from app.task_context import TaskExecution, task_execution
from app.database import schema_lock_sync

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "waiting_human", "failed", "cancelled", "timed_out", "interrupted"}
FAILURES = {"failed", "cancelled", "timed_out", "interrupted"}
LEASE_SECONDS = 30


class TaskStore:
    def __init__(self, database):
        self.db = database

    def setup(self):
        with schema_lock_sync(self.db.url), self.db.connection() as execute:
            for sql in (
                "CREATE TABLE IF NOT EXISTS hp_tasks (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL, thread_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, request_id TEXT NOT NULL, worker_token TEXT, lease_until BIGINT, cancel_requested INTEGER NOT NULL DEFAULT 0, error_code TEXT, UNIQUE(user_id, idempotency_key))",
                "CREATE INDEX IF NOT EXISTS hp_tasks_queue ON hp_tasks(status, created_at)",
                "CREATE INDEX IF NOT EXISTS hp_tasks_user ON hp_tasks(user_id, created_at)",
                "CREATE INDEX IF NOT EXISTS hp_tasks_conversation ON hp_tasks(user_id, thread_id, created_at)",
                "CREATE TABLE IF NOT EXISTS hp_task_events (task_id TEXT NOT NULL REFERENCES hp_tasks(id), seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(task_id, seq))",
                "CREATE TABLE IF NOT EXISTS hp_task_slots (user_id TEXT PRIMARY KEY REFERENCES hp_users(id), task_id TEXT NOT NULL REFERENCES hp_tasks(id))",
                "CREATE TABLE IF NOT EXISTS hp_task_effects (task_id TEXT NOT NULL REFERENCES hp_tasks(id), kind TEXT NOT NULL, payload_hash TEXT NOT NULL, committed_at BIGINT NOT NULL, PRIMARY KEY(task_id, kind))",
                "CREATE TABLE IF NOT EXISTS hp_conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES hp_users(id), thread_id TEXT NOT NULL, title TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0, updated_at BIGINT NOT NULL, UNIQUE(user_id, thread_id))",
                "CREATE INDEX IF NOT EXISTS hp_conversations_user ON hp_conversations(user_id, archived, updated_at)",
                "CREATE TABLE IF NOT EXISTS hp_task_reviews (task_id TEXT PRIMARY KEY REFERENCES hp_tasks(id), disposition TEXT NOT NULL, reviewed_at BIGINT NOT NULL, request_id TEXT NOT NULL)",
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
                "failure_category": {"failed": "execution", "cancelled": "cancelled", "timed_out": "timeout", "interrupted": "infrastructure"}.get(row["status"]),
                "review_disposition": row["review_disposition"] if "review_disposition" in row.keys() else None}

    @property
    def select_public(self):
        # SQLite 任务与内存健康记录不在同一事务内，不能声称已确认持久化。
        committed = "EXISTS (SELECT 1 FROM hp_task_effects e WHERE e.task_id = hp_tasks.id)" if self.db.url else "NULL"
        return f"SELECT hp_tasks.*, {committed} AS health_committed, (SELECT disposition FROM hp_task_reviews r WHERE r.task_id = hp_tasks.id) AS review_disposition FROM hp_tasks"

    @staticmethod
    def _event(execute, task_id, event):
        previous = execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM hp_task_events WHERE task_id = ?", (task_id,)).fetchone()["seq"]
        execute("INSERT INTO hp_task_events VALUES (?, ?, ?)",
                (task_id, previous + 1, json.dumps(event, ensure_ascii=False)))

    def submit(self, user_id, key, payload, request_id):
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        payload_hash = digest(serialized)
        task_id, now = uuid.uuid4().hex, time.time_ns()
        with self.db.connection() as execute:
            execute("INSERT INTO hp_tasks (id, user_id, idempotency_key, payload_hash, thread_id, payload, status, created_at, updated_at, request_id) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?) ON CONFLICT (user_id, idempotency_key) DO NOTHING",
                    (task_id, user_id, key, payload_hash, payload["thread_id"], serialized, now, now, request_id))
            row = execute(self.select_public + " WHERE user_id = ? AND idempotency_key = ?", (user_id, key)).fetchone()
            if row["payload_hash"] != payload_hash:
                raise AuthFailure(409, "IDEMPOTENCY_CONFLICT", "同一幂等键对应不同请求，请勿修改后重用。")
            created = row["id"] == task_id
            if created:
                suffix = " FOR UPDATE" if self.db.url else ""
                conversation = execute("SELECT archived FROM hp_conversations WHERE user_id = ? AND thread_id = ?" + suffix, (user_id, payload["thread_id"])).fetchone()
                if conversation and conversation["archived"]:
                    raise AuthFailure(409, "CONVERSATION_ARCHIVED", "此会话已归档，请先在工作台恢复。")
                execute("INSERT INTO hp_conversations VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT (user_id, thread_id) DO UPDATE SET updated_at = excluded.updated_at",
                        (uuid.uuid4().hex, user_id, payload["thread_id"], "健康会话", now))
                self.db._audit(execute, user_id, "task.submitted", task_id, request_id, 202)
            return self.public(row), created

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
            row = execute("SELECT * FROM hp_tasks WHERE status = 'pending' AND user_id NOT IN (SELECT user_id FROM hp_task_slots) ORDER BY created_at LIMIT 1" + suffix).fetchone()
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

    def finish(self, task_id, token, status, code=None, message=None):
        with self.db.connection() as execute:
            row = self._lock_task(execute, task_id)
            if row and row["status"] == "running" and row["worker_token"] == token:
                if row["cancel_requested"]:
                    status, code, message = "cancelled", "TASK_CANCELLED", "任务已停止，已产生的记录不会回滚。"
                self._finish(execute, row, status, code, message)


class TaskRunner:
    """有界的数据库队列消费者，可嵌入单机 API 或在独立进程运行。"""
    def __init__(self, store, agent, *, workers=2, timeout=300):
        self.store, self.agent = store, agent
        self.workers, self.timeout = workers, timeout
        self.loops = []

    async def start(self):
        await asyncio.to_thread(self.store.recover_stale)
        self.loops = [asyncio.create_task(self.loop()) for _ in range(self.workers)]

    async def close(self):
        for loop in self.loops:
            loop.cancel()
        await asyncio.gather(*self.loops, return_exceptions=True)

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
                logger.error("任务消费者故障 type=%s", type(exc).__name__)
                await asyncio.sleep(1)

    async def run(self, job):
        task_id, token = job["id"], job["worker_token"]
        context = request_id_context.set(job["request_id"])
        execution_context = task_execution.set(TaskExecution(task_id, token, job["user_id"]))
        async def consume():
            payload = json.loads(job["payload"])
            thread = await asyncio.to_thread(self.store.db.claim_thread, payload["thread_id"], job["user_id"])
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
            await asyncio.to_thread(self.store.finish, task_id, token, status,
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
            await asyncio.to_thread(self.store.finish, task_id, token, status, code, message)
        except asyncio.CancelledError:
            execution.cancel()
            with suppress(asyncio.CancelledError):
                await execution
            await asyncio.to_thread(self.store.finish, task_id, token, "interrupted", "WORKER_STOPPED", "执行服务已停止，请核对已有记录。")
            raise
        except Exception as exc:
            execution.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await execution
            logger.error("任务执行失败 task_id=%s type=%s", task_id, type(exc).__name__)
            await asyncio.to_thread(self.store.finish, task_id, token, "failed", "TASK_EXECUTION_FAILED", "任务执行失败，请核对已有记录。")
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await watcher
            request_id_context.reset(context)
            task_execution.reset(execution_context)
