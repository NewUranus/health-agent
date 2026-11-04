"""第一批独立验收；不修改仓库原有 tests/。"""
import json
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import COOKIE_NAME, AuthStore, digest

PASSWORD = "local-validation-password-2026"
FULL = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"
WRITE = {"X-HealthPilot-Request": "1"}


@pytest.fixture
def secured(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_AGENT_MOCK", "1")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "required")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("HEALTH_AGENT_COOKIE_SECURE", "1")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "local-admin-test")
    from app.main import create_app
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        store = app.state.auth_store
        users = {name: store.provision(name, PASSWORD) for name in ("alice", "bob")}
        yield client, store, users


def login(client, username, prefix="/api/v1"):
    response = client.post(prefix + "/auth/login", json={"username": username, "password": PASSWORD}, headers=WRITE)
    assert response.status_code == 200, response.text
    return response, client.cookies.get(COOKIE_NAME)


def headers(token, **extra):
    return {**WRITE, "Cookie": f"{COOKIE_NAME}={token}", **extra}


def events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_anonymous_aliases_errors_and_csrf(secured):
    client, _, _ = secured
    for prefix in ("/api", "/api/v1"):
        for path in ("/history", "/followup"):
            response = client.get(prefix + path)
            assert response.status_code == 401
            assert response.json()["code"] == "AUTH_REQUIRED"
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert response.headers["cache-control"] == "no-store"
        for path, body in (("/chat", {"thread_id": "x", "message": "你好"}), ("/tools/bmi", {"height_cm": 170, "weight_kg": 60})):
            assert client.post(prefix + path, json=body).status_code == 401
        assert client.post(prefix + "/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 403
        assert client.post(prefix + "/auth/login", json={"username": "alice", "password": PASSWORD},
                           headers={**WRITE, "Origin": "https://evil.invalid"}).status_code == 403
        assert client.get(prefix + "/health").status_code == 200
    response, token = login(client, "alice")
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict", "path=/"))
    assert token not in response.text
    assert client.get("/api/auth/me").json()["user"]["username"] == "alice"
    bad = client.post("/api/chat", json={"message": "你好"}, headers=headers(token))
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_ARGUMENT"
    assert client.get("/api/v2/health").status_code == 404
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.0"
    for prefix in ("/api", "/api/v1"):
        assert document["paths"][prefix + "/auth/me"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_identity_thread_history_followup_and_audit(secured):
    client, store, users = secured
    _, a = login(client, "alice")
    a_id, b_id = users["alice"]["id"], users["bob"]["id"]
    response = client.post("/api/chat", json={"thread_id": "alice-thread", "message": "男，55岁"}, headers=headers(a))
    events(response)
    result = events(client.post("/api/v1/chat", json={"thread_id": "alice-thread", "message": FULL}, headers=headers(a)))
    assert any(e.get("node") == "risk_assess" for e in result)
    assert len(client.get("/api/v1/history", headers=headers(a)).json()["records"]) == 1
    assert client.get("/api/followup", headers=headers(a)).json()["status"] != "none"
    _, b = login(client, "bob")
    for prefix in ("/api", "/api/v1"):
        assert client.get(prefix + "/history", headers=headers(b)).json() == {"records": []}
        assert client.get(prefix + "/followup", headers=headers(b)).json() == {"status": "none"}
        for path in ("/history", "/followup"):
            assert client.get(prefix + path, params={"user_id": a_id}, headers=headers(b)).status_code == 403
        assert client.post(prefix + "/chat", json={"thread_id": "new", "user_id": a_id, "message": "你好"}, headers=headers(b)).status_code == 403
        assert client.post(prefix + "/chat", json={"thread_id": "alice-thread", "message": "你好"}, headers=headers(b)).status_code == 404
        assert client.get(prefix + "/history", headers=headers(b, **{"X-HealthPilot-User": a_id})).json()["code"] == "SESSION_CHANGED"
    result = events(client.post("/api/chat", json={"thread_id": "bob-thread", "user_id": b_id, "message": "你好"}, headers=headers(b)))
    assert next(e for e in result if e.get("node") == "intake")["data"]["profile"] == {}
    # 切换客户端 Cookie 后重新登录 A，仍然只看到 A 的历史。
    _, a2 = login(client, "alice")
    assert len(client.get("/api/history", headers=headers(a2)).json()["records"]) == 1
    with store.connection() as execute:
        records = [dict(r) for r in execute("SELECT * FROM hp_audit").fetchall()]
        persisted = [dict(r) for r in execute("SELECT * FROM hp_sessions").fetchall()]
    raw = json.dumps(records)
    assert PASSWORD not in raw and a not in raw and b not in raw and FULL not in raw
    assert any(r["action"] == "chat.completed" for r in records)
    assert any(r["status"] == 403 for r in records)
    assert all(r["request_id"] for r in records)
    assert all(r["token_hash"] not in (a, b) for r in persisted)
    thread_name = store.claim_thread("alice-thread", a_id)
    trace = (Path(__file__).resolve().parents[1] / "runs" / "traces" / "accounts" / (thread_name + ".jsonl")).read_text()
    assert response.headers["x-request-id"] in trace
    assert '"profile"' not in trace and '"args"' not in trace


def test_logout_expiry_rotation_and_reset(secured):
    client, store, _ = secured
    _, token = login(client, "alice")
    _, rotated = login(client, "alice", "/api")
    assert not store.resolve(token) and store.resolve(rotated)
    assert client.post("/api/auth/logout", headers=headers(rotated)).status_code == 200
    assert client.get("/api/v1/history", headers=headers(rotated)).status_code == 401
    _, expired = login(client, "alice")
    with store.connection() as execute:
        execute("UPDATE hp_sessions SET expires_at = ? WHERE token_hash = ?", (int(time.time()) - 1, digest(expired)))
    assert client.get("/api/auth/me", headers=headers(expired)).status_code == 401
    _, token = login(client, "alice")
    store.provision("alice", "different-validation-password", reset=True)
    assert client.get("/api/history", headers=headers(token)).status_code == 401
    assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers=WRITE).status_code == 401


def test_passwords_limits_persistence_and_admin_boundary(secured, monkeypatch):
    client, store, users = secured
    with store.connection() as execute:
        stored = execute("SELECT password_hash FROM hp_users WHERE id = ?", (users["alice"]["id"],)).fetchone()["password_hash"]
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    for _ in range(8):
        response = client.post("/api/auth/login", json={"username": "missing", "password": "wrong"}, headers=WRITE)
        assert response.status_code == 401 and response.json()["detail"] == "账号或密码错误。"
    assert client.post("/api/auth/login", json={"username": "missing", "password": "wrong"}, headers=WRITE).status_code == 429
    _, token = login(client, "alice")
    reconnected = AuthStore()
    reconnected.setup()
    assert reconnected.resolve(token)["id"] == users["alice"]["id"]
    for prefix in ("/api", "/api/v1"):
        # 普通登录会话不能进后台；无共享口令时返回无权，而不是伪装成未登录。
        denied = client.get(prefix + "/admin/documents", headers=headers(token))
        assert denied.status_code == 403 and denied.json()["code"] == "ACCESS_DENIED"
        assert client.get(prefix + "/admin/documents", headers={"Authorization": "Bearer local-admin-test"}).status_code == 200
    client.cookies.clear()
    monkeypatch.delenv("ADMIN_PASSWORD")
    assert client.get("/api/admin/documents").json()["code"] == "ADMIN_DISABLED"


def test_demo_namespace_and_configuration(secured, monkeypatch):
    client, _, users = secured
    from app.http_security import auth_required, current_identity
    from starlette.requests import Request
    assert auth_required(False)
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "demo")
    with pytest.raises(ValueError):
        auth_required(False)
    assert not auth_required(True)
    request = Request({"type": "http", "app": client.app})
    client.app.state.auth_required = False
    try:
        assert current_identity(request, "account:" + users["alice"]["id"]).startswith("demo:account:")
    finally:
        client.app.state.auth_required = True


def test_audit_unavailable_fails_closed(secured, monkeypatch):
    client, store, _ = secured
    _, token = login(client, "alice")
    def broken(*args):
        raise RuntimeError("内部故障内容不应返回")
    monkeypatch.setattr(store, "audit", broken)
    response = client.post("/api/chat", json={"thread_id": "must-not-start", "message": FULL}, headers=headers(token))
    assert response.status_code == 503
    assert "内部故障" not in response.text
    with store.connection() as execute:
        assert execute("SELECT * FROM hp_threads WHERE thread_hash = ?", (digest("must-not-start"),)).fetchone() is None


def test_concurrent_thread_claim_and_request_ids(secured):
    client, store, users = secured
    from app.auth import AuthFailure
    def claim(name):
        try:
            return store.claim_thread("racing-thread", users[name]["id"])
        except AuthFailure as exc:
            return exc.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("alice", "bob")))
    assert outcomes.count(404) == 1
    assert sum(isinstance(value, str) for value in outcomes) == 1
    responses = [client.get("/api/health", headers={"X-Request-ID": "untrusted-caller-value"}) for _ in range(2)]
    ids = [r.headers["x-request-id"] for r in responses]
    assert len(set(ids)) == 2 and "untrusted-caller-value" not in ids


def test_login_schema_and_documented_auth(secured):
    client, _, _ = secured
    response = client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD, "admin": True}, headers=WRITE)
    assert response.status_code == 422 and PASSWORD not in response.text
    schema = client.get("/openapi.json").json()
    for prefix in ("/api", "/api/v1"):
        operation = schema["paths"][prefix + "/chat"]["post"]
        assert operation["security"] == [{"UserSession": []}]
        assert any(p["name"] == "X-HealthPilot-Request" and p["required"] for p in operation["parameters"])
