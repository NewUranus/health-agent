"""免登录体验：临时游客会话、额度扣减与回滚、高级功能需登录、按网络限流、运维暂停与到期清理。"""
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import guests
from app.auth import COOKIE_NAME

PASSWORD = "local-guest-validation-password"
WRITE = {"X-HealthPilot-Request": "1"}
MESSAGE = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_AGENT_MOCK", "1")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "required")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("HEALTH_AGENT_COOKIE_SECURE", "0")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("HEALTH_AGENT_GUEST_MESSAGES", "2")
    monkeypatch.setenv("HEALTH_AGENT_GUEST_DAILY_MESSAGES", "3")
    monkeypatch.setenv("HEALTH_AGENT_GUEST_PER_IP", "3")
    from app.main import create_app
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        store = app.state.auth_store
        store.provision("patient", PASSWORD, role="user")
        store.provision("opsadmin", PASSWORD, role="ops_admin")
        yield app, client, store


def fresh(app):
    return TestClient(app, base_url="https://testserver")


def start_guest(client):
    response = client.post("/api/v1/auth/guest", headers=WRITE)
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, key, message=MESSAGE, thread="guest-thread", web_search=False):
    return client.post("/api/v1/tasks", json={"thread_id": thread, "message": message, "web_search": web_search},
                       headers={**WRITE, "Idempotency-Key": key})


def wait(client, task_id):
    for _ in range(300):
        status = client.get(f"/api/v1/tasks/{task_id}").json()["status"]
        if status not in {"pending", "running"}:
            return status
        time.sleep(0.03)
    raise AssertionError("任务未结束")


def test_homepage_can_enter_guest_mode(site):
    app, client, _ = site
    me = client.get("/api/v1/auth/me")
    assert me.status_code == 401 and me.headers["X-HealthPilot-Guest"] == "available"
    body = start_guest(client)
    assert body["user"]["role"] == "guest" and body["user"]["username"].startswith("guest-")
    assert body["guest"] == {"limit": 2, "remaining": 2, "hours": 24, "daily_available": True}
    assert client.cookies.get(COOKIE_NAME)
    me = client.get("/api/v1/auth/me").json()
    assert me["user"]["id"] == body["user"]["id"] and me["guest"]["remaining"] == 2
    # 已是游客时再次请求沿用同一会话，不新建账号
    again = client.post("/api/v1/auth/guest", headers=WRITE)
    assert again.status_code == 200 and again.json()["user"]["id"] == body["user"]["id"]
    # 跨站写请求同样被拒绝
    assert fresh(app).post("/api/v1/auth/guest").json()["code"] == "CSRF_REJECTED"


def test_quota_counts_new_tasks_only_and_rolls_back_rejections(site):
    _, client, _ = site
    start_guest(client)
    first = submit(client, "guest-key-0001")
    assert first.status_code == 202
    assert submit(client, "guest-key-0001").status_code == 200  # 幂等重放不扣额度
    assert client.get("/api/v1/auth/me").json()["guest"]["remaining"] == 1
    blocked = submit(client, "guest-key-0002", web_search=True)
    assert blocked.status_code == 403 and blocked.json()["code"] == "LOGIN_REQUIRED"
    assert client.get("/api/v1/auth/me").json()["guest"]["remaining"] == 1  # 被拒绝的提交不占额度
    assert wait(client, first.json()["id"]) == "succeeded"
    assert submit(client, "guest-key-0003", "高血压吃什么好").status_code == 202
    exhausted = submit(client, "guest-key-0004", "怎么测血压")
    assert exhausted.status_code == 429 and exhausted.json()["code"] == "GUEST_QUOTA_EXHAUSTED"
    assert client.get("/api/v1/auth/me").json()["guest"]["remaining"] == 0


def test_advanced_features_require_login(site):
    _, client, _ = site
    guest = start_guest(client)
    for method, path, kwargs in [
        ("get", "/api/v1/conversations", {}),
        ("get", "/api/conversations/abc/export", {}),
        ("post", "/api/v1/chat", {"json": {"thread_id": "t", "message": "你好"}}),
        ("post", "/api/v1/auth/password", {"json": {"current_password": "x" * 12, "new_password": "y" * 12}}),
        ("post", "/api/v1/tools/cvd_risk", {"json": {}}),
    ]:
        response = getattr(client, method)(path, headers=WRITE, **kwargs)
        assert response.status_code == 403 and response.json()["code"] == "LOGIN_REQUIRED", path
    assert client.get("/api/v1/admin/operations").status_code == 403
    assert client.get("/api/v1/followup").status_code == 200  # 随访记录仍可用
    # 游客账号没有密码，不能用账号名登录
    login = client.post("/api/v1/auth/login", json={"username": guest["user"]["username"], "password": "anything-long-enough"}, headers=WRITE)
    assert login.status_code == 401


def test_signing_in_replaces_guest_session(site):
    _, client, _ = site
    start_guest(client)
    guest_cookie = client.cookies.get(COOKIE_NAME)
    response = client.post("/api/v1/auth/login", json={"username": "patient", "password": PASSWORD}, headers=WRITE)
    assert response.status_code == 200 and response.json()["user"]["role"] == "user"
    stale = client.get("/api/v1/auth/me", headers={"Cookie": f"{COOKIE_NAME}={guest_cookie}"})
    assert stale.status_code == 401
    # 已登录账号不能再开启免登录体验
    assert client.post("/api/v1/auth/guest", headers=WRITE).json()["code"] == "ALREADY_SIGNED_IN"


def test_guest_accounts_cannot_be_managed_or_listed(site):
    _, client, store = site
    guest = start_guest(client)
    with pytest.raises(ValueError):
        store.provision("guest-forged", PASSWORD)
    with pytest.raises(ValueError):
        store.assign_role(guest["user"]["username"], "user")
    admin = fresh(client.app)
    admin.post("/api/v1/auth/login", json={"username": "opsadmin", "password": PASSWORD}, headers=WRITE)
    names = [row["username"] for row in admin.get("/api/v1/admin/accounts").json()["accounts"]]
    assert "patient" in names and not any(name.startswith("guest-") for name in names)


def test_per_network_and_daily_limits(site):
    app, client, _ = site
    for _ in range(3):
        start_guest(fresh(app))
    limited = fresh(app).post("/api/v1/auth/guest", headers=WRITE)
    assert limited.status_code == 429 and limited.json()["code"] == "GUEST_LIMITED"

    # 以下检查全站每日额度，先清掉按网络的计数
    with app.state.auth_store.connection() as execute:
        execute("DELETE FROM hp_guest_usage WHERE scope LIKE 'guest-ip:%'")
    start_guest(client)
    assert submit(client, "daily-key-0001").status_code == 202
    assert submit(client, "daily-key-0002", "高血压吃什么").status_code == 202
    # 全站当天 3 条，第一个游客用掉 2 条后换另一个游客
    second = fresh(app)
    start_guest(second)
    assert submit(second, "daily-key-0003", thread="second-thread").status_code == 202
    exhausted = submit(second, "daily-key-0004", "怎么测血压", thread="second-thread")
    assert exhausted.status_code == 429 and exhausted.json()["code"] == "GUEST_DAILY_EXHAUSTED"
    assert "X-HealthPilot-Guest" not in fresh(app).get("/api/v1/auth/me").headers
    assert second.get("/api/v1/auth/me").json()["guest"]["daily_available"] is False


def test_client_ip_trusts_only_configured_proxies(monkeypatch):
    def request(peer, forwarded=""):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": forwarded} if forwarded else {})
    assert guests.client_ip(request("172.18.0.5", "203.0.113.9, 172.18.0.5")) == "203.0.113.9"
    assert guests.client_ip(request("172.18.0.5", "198.51.100.1, 203.0.113.9")) == "203.0.113.9"  # 伪造的左侧地址不采信
    assert guests.client_ip(request("8.8.8.8", "203.0.113.9")) == "8.8.8.8"  # 不是受信代理时忽略转发头
    assert guests.client_ip(request("172.18.0.5", "not-an-ip")) == "172.18.0.5"
    monkeypatch.setenv("HEALTH_AGENT_TRUSTED_PROXIES", "10.1.0.0/16")
    assert guests.client_ip(request("172.18.0.5", "203.0.113.9")) == "172.18.0.5"


def test_ops_admin_can_pause_guest_mode(site):
    app, client, _ = site
    guest = fresh(app)
    start_guest(guest)
    client.post("/api/v1/auth/login", json={"username": "opsadmin", "password": PASSWORD}, headers=WRITE)
    assert client.patch("/api/v1/admin/guest", json={"paused": True}, headers=WRITE).json() == {"paused": True}
    overview = client.get("/api/v1/admin/operations").json()["guest"]
    assert overview["paused"] is True and overview["active"] == 1 and overview["daily_limit"] == 3
    assert fresh(app).post("/api/v1/auth/guest", headers=WRITE).json()["code"] == "GUEST_PAUSED"
    assert submit(guest, "paused-key-0001").json()["code"] == "GUEST_PAUSED"
    assert "X-HealthPilot-Guest" not in fresh(app).get("/api/v1/auth/me").headers
    client.patch("/api/v1/admin/guest", json={"paused": False}, headers=WRITE)
    assert submit(guest, "paused-key-0002").status_code == 202


def test_expired_guests_are_purged_with_their_health_data(site):
    app, client, store = site
    guest = start_guest(client)["user"]
    task = submit(client, "purge-key-0001").json()
    assert wait(client, task["id"]) == "succeeded"
    agent = app.state.agent
    namespace = ("users", "account:" + guest["id"])
    assert asyncio.run(agent.store.aget(namespace, "profile")) is not None

    async def purge(now):
        return await guests.purge_expired(store, agent.store, agent.checkpointer, now=now)
    assert asyncio.run(purge(int(time.time()))) == 0  # 未到期不删
    later = int(time.time()) + 24 * 3600 + guests.PURGE_GRACE + 10
    assert asyncio.run(purge(later)) == 1
    with store.connection() as execute:
        for table in ("hp_users", "hp_sessions", "hp_tasks", "hp_conversations", "hp_threads"):
            column = "id" if table == "hp_users" else "user_id"
            assert execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (guest["id"],)).fetchone()["n"] == 0, table
        assert execute("SELECT COUNT(*) AS n FROM hp_task_events WHERE task_id = ?", (task["id"],)).fetchone()["n"] == 0
        assert execute("SELECT COUNT(*) AS n FROM hp_audit WHERE action = 'guest.purged' AND resource = ?", (guest["id"],)).fetchone()["n"] == 1
        assert execute("SELECT COUNT(*) AS n FROM hp_users WHERE username = 'patient'").fetchone()["n"] == 1
    assert asyncio.run(agent.store.aget(namespace, "profile")) is None
    assert client.get("/api/v1/auth/me").status_code == 401
