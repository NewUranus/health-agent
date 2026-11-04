"""任务队列、幂等和隔离验收；只使用临时数据库及虚拟健康数据。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

import pytest

from app.auth import AuthStore
from app.tasks import TaskStore, TERMINAL
from test_foundation import secured, login, headers, FULL


def submit(client, token, key, message=FULL, thread="task-thread", prefix="/api/v1"):
    return client.post(prefix + "/tasks", json={"thread_id": thread, "message": message},
                       headers={**headers(token), "Idempotency-Key": key})


def finished(client, token, task_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get("/api/tasks/" + task_id, headers=headers(token))
        assert response.status_code == 200, response.text
        if response.json()["status"] in TERMINAL:
            return response.json()
        time.sleep(0.03)
    pytest.fail("任务未在验收期限内结束")


@pytest.fixture
def task_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "tasks.sqlite3"))
    database = AuthStore()
    database.setup()
    user = database.provision("task-user", "task-validation-password")
    store = TaskStore(database)
    store.setup()
    return store, user["id"]


def test_task_idempotency_replay_and_assessment_once(secured):
    client, store, _ = secured
    _, token = login(client, "alice")
    response = submit(client, token, "assessment-once")
    assert response.status_code == 202
    task_id = response.json()["id"]
    result = finished(client, token, task_id)
    assert result["status"] == "succeeded"
    duplicate = submit(client, token, "assessment-once", prefix="/api")
    assert duplicate.status_code == 200 and duplicate.json()["id"] == task_id
    assert len(client.get("/api/history", headers=headers(token)).json()["records"]) == 1
    assert submit(client, token, "assessment-once", message="不同内容").status_code == 409
    page = client.get(f"/api/tasks/{task_id}/event-page", headers=headers(token)).json()
    sequences = [row["seq"] for row in page["events"]]
    assert sequences == list(range(1, len(sequences) + 1))
    assert page["events"][-1]["event"] == {"type": "done"}
    stream = client.get(f"/api/v1/tasks/{task_id}/events?after=1", headers=headers(token))
    assert stream.status_code == 200 and 'id: 1\n' not in stream.text and 'id: 2\n' in stream.text
    cursor = sequences[-1]
    assert client.get(f"/api/tasks/{task_id}/event-page?after={cursor}", headers=headers(token)).json()["events"] == []
    with store.connection() as execute:
        actions = [row["action"] for row in execute("SELECT action FROM hp_audit WHERE resource = ?", (task_id,)).fetchall()]
    assert actions.count("task.submitted") == 1 and actions.count("task.succeeded") == 1


def test_task_auth_and_cross_account_access(secured):
    client, _, _ = secured
    for prefix in ("/api", "/api/v1"):
        assert client.get(prefix + "/tasks").status_code == 401
        assert submit(client, "invalid", "unauthorized").status_code == 401
    _, alice = login(client, "alice")
    response = submit(client, alice, "private-task")
    task_id = response.json()["id"]
    finished(client, alice, task_id)
    _, bob = login(client, "bob")
    for prefix in ("/api", "/api/v1"):
        assert client.get(prefix + "/tasks", headers=headers(bob)).json()["tasks"] == []
        for suffix in ("", "/events", "/event-page"):
            assert client.get(prefix + "/tasks/" + task_id + suffix, headers=headers(bob)).status_code == 404
        assert client.post(prefix + "/tasks/" + task_id + "/cancel", headers=headers(bob)).status_code == 404
        assert submit(client, bob, "other-private-task", thread="task-thread", prefix=prefix).status_code == 404
    missing = client.post("/api/tasks", headers=headers(bob), json={"thread_id": "bob", "message": "你好"})
    assert missing.status_code == 422


def test_running_cancellation_and_no_browser_subscription(secured, monkeypatch):
    client, _, _ = secured
    async def blocked(*args, **kwargs):
        yield {"type": "node", "node": "intake", "agent": "orchestrator", "data": {}, "ms": 1}
        await asyncio.sleep(20)
        yield {"type": "message", "content": "不应到达"}
        yield {"type": "done"}
    monkeypatch.setattr(client.app.state.agent, "stream", blocked)
    _, token = login(client, "alice")
    task_id = submit(client, token, "cancel-running").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get("/api/tasks/" + task_id, headers=headers(token)).json()["status"] == "running": break
        time.sleep(0.03)
    cancelled = client.post(f"/api/tasks/{task_id}/cancel", headers=headers(token))
    assert cancelled.status_code == 200
    result = finished(client, token, task_id)
    assert result["status"] == "cancelled"
    page = client.get(f"/api/tasks/{task_id}/event-page", headers=headers(token)).json()
    assert all(row["event"].get("content") != "不应到达" for row in page["events"])


def test_followup_waiting_human_can_continue(secured):
    client, _, _ = secured
    _, token = login(client, "alice")
    first = submit(client, token, "initial-assessment").json()["id"]
    assert finished(client, token, first)["status"] == "succeeded"
    followup = submit(client, token, "followup-start", message="我来复查，最近血压130/85").json()["id"]
    assert finished(client, token, followup)["status"] == "waiting_human"
    cancelled = submit(client, token, "followup-cancel", message="取消").json()["id"]
    assert finished(client, token, cancelled)["status"] == "succeeded"
    assert len(client.get("/api/history", headers=headers(token)).json()["records"]) == 1


def test_concurrent_submit_and_restart_recovery(task_database):
    store, user = task_database
    payload = {"thread_id": "persisted", "message": "你好", "web_search": False}
    def create(_):
        return store.submit(user, "same-key", payload, uuid.uuid4().hex)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(8)))
    assert len({task["id"] for task, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    reconnected = TaskStore(AuthStore())
    reconnected.setup()
    job = reconnected.claim("worker-one")
    assert job and job["id"] == results[0][0]["id"]
    assert reconnected.claim("worker-two") is None
    with store.db.connection() as execute:
        execute("UPDATE hp_tasks SET lease_until = 0 WHERE id = ?", (job["id"],))
    reconnected.recover_stale()
    assert reconnected.get(job["id"], user)["status"] == "interrupted"
    assert not reconnected.append(job["id"], "worker-one", {"type": "message", "content": "过期写入"})
    assert reconnected.claim("worker-two") is None
    assert reconnected.submit(user, "same-key", payload, "again")[0]["status"] == "interrupted"


def test_user_queue_slot_and_pending_cancel(task_database):
    store, user = task_database
    payload = {"thread_id": "serial", "message": "你好"}
    first = store.submit(user, "first-key", payload, "request-one")[0]
    second = store.submit(user, "second-key", payload, "request-two")[0]
    job = store.claim("worker")
    assert job["id"] == first["id"]
    assert store.claim("another-worker") is None
    assert store.cancel(second["id"], user, "cancel-request")["status"] == "cancelled"
    store.finish(first["id"], "wrong-token", "succeeded")
    assert store.get(first["id"], user)["status"] == "running"
    store.finish(first["id"], "worker", "succeeded")
    assert store.claim("worker") is None
    _, rows = store.events(second["id"], user)
    assert rows[-1]["event"]["type"] == "done"


def test_timeout_and_stale_worker_cannot_finish(task_database):
    from app.tasks import TaskRunner
    store, user = task_database
    store.submit(user, "timeout-key", {"thread_id": "timeout", "message": "你好"}, "timeout-request")
    job = store.claim("worker")
    class SlowAgent:
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(10)
            yield {"type": "done"}
    asyncio.run(TaskRunner(store, SlowAgent(), timeout=0.1).run(job))
    assert store.get(job["id"], user)["status"] == "timed_out"
    store.finish(job["id"], "worker", "succeeded")
    assert store.get(job["id"], user)["status"] == "timed_out"
