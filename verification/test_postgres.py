"""显式开启的 PostgreSQL/pgvector 多进程验收；每例独占并清理临时数据库。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app.auth import AuthStore
from app.tasks import TaskStore, TERMINAL
from app.task_context import TaskExecution, task_execution
from app.memory import memory_backend, load_memory, load_followup, save_assessment

ROOT = Path(__file__).resolve().parents[1]
FULL = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"
PASSWORD = "pg-verification-password"
WRITE = {"X-HealthPilot-Request": "1"}


def eventually(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("未在期限内达到预期状态")


@pytest.fixture
def pg(tmp_path, monkeypatch):
    base = os.getenv("HEALTH_AGENT_TEST_POSTGRES")
    if not base:
        pytest.skip("需显式配置 HEALTH_AGENT_TEST_POSTGRES 到可创建临时数据库的测试服务")
    name = "hp_verify_" + uuid.uuid4().hex
    with psycopg.connect(base, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(base, dbname=name)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("HEALTH_AGENT_MOCK", "1")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "required")
    monkeypatch.setenv("HEALTH_AGENT_COOKIE_SECURE", "0")
    monkeypatch.setenv("HEALTH_AGENT_TASK_WORKER", "external")
    database = AuthStore()
    database.setup()
    tasks = TaskStore(database)
    tasks.setup()
    user = database.provision("pg-user", PASSWORD)
    processes = []
    files = []

    def spawn(*args):
        log = (tmp_path / f"process-{len(processes)}.log").open("w+")
        files.append(log)
        process = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=os.environ.copy(),
                                   stdout=log, stderr=log, start_new_session=True)
        processes.append(process)
        return process

    def stop(process, force=False):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGINT)
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def api():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = spawn("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port))
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=WRITE, timeout=20)
        def ready():
            if process.poll() is not None:
                raise AssertionError("API 启动失败，见临时进程日志")
            try:
                return client.get("/api/health").status_code == 200
            except httpx.TransportError:
                return False
        eventually(ready)
        response = client.post("/api/v1/auth/login", json={"username": "pg-user", "password": PASSWORD})
        assert response.status_code == 200, response.text
        return process, client

    try:
        yield database, tasks, user["id"], spawn, stop, api
    finally:
        for process in reversed(processes):
            stop(process)
        for log in files:
            log.close()
        with psycopg.connect(base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def submit(client, key, message=FULL, thread="pg-thread"):
    response = client.post("/api/v1/tasks", json={"thread_id": thread, "message": message},
                           headers={"Idempotency-Key": key})
    assert response.status_code in (200, 202), response.text
    return response.json()


def final(tasks, task_id, user):
    return eventually(lambda: (row if (row := tasks.get(task_id, user))["status"] in TERMINAL else None))


def test_postgres_two_workers_http_idempotency_and_restart(pg):
    database, tasks, user, spawn, stop, api = pg
    server, client = api()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: submit(client, "concurrent-key"), range(6)))
    assert len({row["id"] for row in results}) == 1
    job = results[0]
    assert job["status"] == "pending"
    # API 停机不会丢掉队列，独立 Worker 可以完成已提交任务。
    client.close()
    stop(server)
    workers = [spawn("-m", "app.worker") for _ in range(2)]
    result = final(tasks, job["id"], user)
    assert result["status"] == "succeeded", result
    assert all(worker.poll() is None for worker in workers)
    assert result["health_committed"]
    server, client = api()
    assert submit(client, "concurrent-key")["id"] == job["id"]
    conflict = client.post("/api/tasks", json={"thread_id": "pg-thread", "message": "变更参数"}, headers={"Idempotency-Key": "concurrent-key"})
    assert conflict.status_code == 409
    history = client.get("/api/history").json()["records"]
    assert len(history) == 1 and history[0]["task_id"] == job["id"]
    page = client.get(f'/api/tasks/{job["id"]}/event-page').json()
    assert [row["seq"] for row in page["events"]] == list(range(1, len(page["events"]) + 1))
    assert page["events"][-1]["event"]["type"] == "done"
    # 随访问诊检查点跨 Worker 重启继续，且初次评估不重复。
    followup = submit(client, "followup-key", "我来复查，血压130/85")
    assert final(tasks, followup["id"], user)["status"] == "waiting_human"
    for worker in workers:
        stop(worker)
    spawn("-m", "app.worker")
    cancel = submit(client, "followup-cancel", "取消")
    assert final(tasks, cancel["id"], user)["status"] == "succeeded"
    assert len(client.get("/api/history").json()["records"]) == 1
    # 多个不同任务与旧直接接口并发执行，所有历史记录都应保留。
    second_worker = spawn("-m", "app.worker")
    queued = [submit(client, f"distinct-{i}", thread=f"distinct-thread-{i}") for i in range(3)]
    response = client.post("/api/chat", json={"thread_id": "legacy-thread", "message": FULL})
    assert response.status_code == 200 and '"type": "error"' not in response.text
    for item in queued:
        assert final(tasks, item["id"], user)["status"] == "succeeded"
    assert second_worker.poll() is None
    assert len(client.get("/api/history").json()["records"]) == 5
    client.close()


@pytest.mark.parametrize("mode", ["before_commit", "after_commit"])
def test_postgres_process_crash_and_effect_reconciliation(pg, tmp_path, mode):
    database, tasks, user, spawn, stop, api = pg
    server, client = api()
    job = submit(client, "crash-key")
    marker = tmp_path / "health-write-reached"
    worker = spawn("-m", "verification.pg_worker_probe", mode, str(marker))
    eventually(marker.exists)
    stop(worker, force=True)
    spawn("-m", "app.worker")
    result = final(tasks, job["id"], user)
    assert result["status"] == "interrupted"
    assert result["health_committed"] is (mode == "after_commit")
    records = client.get("/api/history").json()["records"]
    assert len(records) == (1 if mode == "after_commit" else 0)
    assert submit(client, "crash-key")["id"] == job["id"]
    assert len(client.get("/api/history").json()["records"]) == len(records)
    # 旧租约不能继续追加事件或覆盖终态。
    with database.connection() as execute:
        token = execute("SELECT worker_token FROM hp_tasks WHERE id = ?", (job["id"],)).fetchone()["worker_token"]
    assert not tasks.append(job["id"], token, {"type": "message", "content": "迟到结果"})
    tasks.finish(job["id"], token, "succeeded")
    assert tasks.get(job["id"], user)["status"] == "interrupted"
    client.close()


def test_postgres_atomic_health_write_fencing_and_deduplication(pg, monkeypatch):
    database, tasks, user, *_ = pg
    tasks.submit(user, "write-key", {"thread_id": "write-thread", "message": "测试"}, "request")
    job = tasks.claim("worker-token")
    profile = {"sbp": 140, "dbp": 90}
    risk = {"risk_percent": 13.5, "level": "intermediate"}
    plan = {"due_date": "2027-01-01"}
    async def scenario():
        async with memory_backend() as (_, store):
            context = task_execution.set(TaskExecution(job["id"], "worker-token", user))
            try:
                import app.memory as memory
                original = memory._write_assessment
                async def fail_after_writes(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise RuntimeError("注入事务提交前失败")
                with monkeypatch.context() as patch:
                    patch.setattr(memory, "_write_assessment", fail_after_writes)
                    with pytest.raises(RuntimeError, match="注入"):
                        await save_assessment(store, "account:" + user, profile, risk, plan)
                assert await load_memory(store, "account:" + user) == ({}, [])
                assert await load_followup(store, "account:" + user) is None
                assert not tasks.get(job["id"], user)["health_committed"]
                await save_assessment(store, "account:" + user, profile, risk, plan)
                await save_assessment(store, "account:" + user, profile, risk, plan)
                assert len((await load_memory(store, "account:" + user))[1]) == 1
                assert tasks.get(job["id"], user)["health_committed"]
                with pytest.raises(RuntimeError, match="参数发生变化"):
                    await save_assessment(store, "account:" + user, {"sbp": 160}, risk, plan)
                tasks.cancel(job["id"], user, "cancel")
                with pytest.raises(RuntimeError, match="失去写入资格"):
                    await save_assessment(store, "account:" + user, profile, risk, plan)
            finally:
                task_execution.reset(context)
    asyncio.run(scenario())


def test_postgres_cross_process_user_lock_and_queue_claim(pg, tmp_path):
    database, tasks, user, spawn, stop, _ = pg
    marker = tmp_path / "lock-held"
    process = spawn("-c", "import asyncio,sys; from pathlib import Path; from app.user_lock import user_execution_lock\nasync def main():\n async with user_execution_lock(sys.argv[1]):\n  Path(sys.argv[2]).touch()\n  await asyncio.sleep(120)\nasyncio.run(main())", "account:" + user, str(marker))
    eventually(marker.exists)
    async def check_lock():
        from app.user_lock import user_execution_lock
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.3), user_execution_lock("account:" + user):
                pytest.fail("跨进程锁失效")
        async with asyncio.timeout(2), user_execution_lock("account:other-user"):
            pass
    asyncio.run(check_lock())
    stop(process, force=True)
    async def released():
        from app.user_lock import user_execution_lock
        async with asyncio.timeout(3), user_execution_lock("account:" + user):
            pass
    asyncio.run(released())
    for index in range(4):
        tasks.submit(user, f"queued-{index}", {"thread_id": "queue", "message": "测试"}, "request")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda n: tasks.claim(f"worker-{n}"), range(4)))
    claimed = [job for job in jobs if job]
    assert len(claimed) == 1
    job = claimed[0]
    tasks.finish(job["id"], job["worker_token"], "succeeded")
    assert tasks.claim("next-worker") is not None


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_postgres_running_cancellation_and_timeout_preserve_committed_health(pg, tmp_path, monkeypatch, action):
    database, tasks, user, spawn, stop, api = pg
    _, client = api()
    pending = submit(client, "pending-cancel")
    response = client.post(f'/api/tasks/{pending["id"]}/cancel')
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    job = submit(client, "running-job")
    marker = tmp_path / "committed"
    if action == "timeout":
        monkeypatch.setenv("HEALTH_AGENT_TASK_TIMEOUT", "3")
    spawn("-m", "verification.pg_worker_probe", "after_commit", str(marker))
    eventually(marker.exists)
    if action == "cancel":
        response = client.post(f'/api/tasks/{job["id"]}/cancel')
        assert response.status_code == 200
    result = final(tasks, job["id"], user)
    assert result["status"] == ("cancelled" if action == "cancel" else "timed_out")
    assert result["health_committed"]
    assert len(client.get("/api/history").json()["records"]) == 1
    assert submit(client, "running-job")["status"] == result["status"]
    client.close()


def test_postgres_concurrent_worker_cold_start(pg, tmp_path):
    _, tasks, user, spawn, _, _ = pg
    markers = [tmp_path / f"worker-ready-{i}" for i in range(3)]
    workers = [spawn("-m", "verification.pg_worker_probe", "ready", str(marker)) for marker in markers]
    def ready():
        assert all(worker.poll() is None for worker in workers), "并发启动有 Worker 退出"
        return all(marker.exists() for marker in markers)
    eventually(ready)
    task = tasks.submit(user, "cold-start", {"thread_id": "cold-start", "message": FULL}, "request")[0]
    assert final(tasks, task["id"], user)["status"] == "succeeded"
