"""管理员在页面上开户、改角色和重置密码；密码不进入响应或审计。"""
from test_roles import ADMIN, PASSWORD, cookie, login, roles

NEW_PASSWORD = "account-admin-password-2026"


def test_both_admin_roles_can_create_without_leaking_password(roles):
    client, store, accounts = roles
    _, knowledge = login(client, "knowadmin")
    for prefix, name in (("/api", "New.Nurse"), ("/api/v1", "Ward.Clerk")):
        created = client.post(prefix + "/admin/accounts", headers=cookie(knowledge),
                              json={"username": name, "password": NEW_PASSWORD, "role": "user"})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["username"] == name.lower() and body["role"] == "user"
        assert "password" not in body and NEW_PASSWORD not in created.text
        listed = client.get(prefix + "/admin/accounts", headers=cookie(knowledge)).json()["accounts"]
        assert any(row["username"] == name.lower() and row["role"] == "user" and "password" not in row for row in listed)
        with store.connection() as execute:
            row = dict(execute("SELECT actor, action, resource FROM hp_audit WHERE action = 'user.created' AND resource = ?",
                               (body["id"],)).fetchone())
        assert row["actor"] == accounts["knowledge"]["id"]
        assert NEW_PASSWORD not in row["action"] and NEW_PASSWORD not in row["resource"]
        duplicate = client.post(prefix + "/admin/accounts", headers=cookie(knowledge),
                                json={"username": name.lower(), "password": NEW_PASSWORD, "role": "user"})
        assert duplicate.status_code == 409 and duplicate.json()["code"] == "ACCOUNT_EXISTS"
        assert NEW_PASSWORD not in duplicate.text


def test_ops_can_manage_accounts_but_not_documents(roles):
    client, store, accounts = roles
    _, ops = login(client, "opsadmin")
    headers = cookie(ops)
    assert client.get("/api/v1/admin/documents", headers=headers).status_code == 403
    created = client.post("/api/v1/admin/accounts", headers=headers,
                          json={"username": "ward-user", "password": NEW_PASSWORD, "role": "knowledge_admin"})
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "knowledge_admin"
    token, _, _ = store.login("ward-user", NEW_PASSWORD, "test", None, "role-check")
    changed = client.patch("/api/v1/admin/accounts/ward-user/role", headers=headers, json={"role": "ops_admin"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["changed"] is True and changed.json()["role"] == "ops_admin"
    assert store.resolve(token) is None
    same = client.patch("/api/v1/admin/accounts/ward-user/role", headers=headers, json={"role": "ops_admin"})
    assert same.status_code == 200 and same.json()["changed"] is False
    with store.connection() as execute:
        row = dict(execute("SELECT actor, action FROM hp_audit WHERE action = ?",
                           ("user.role_changed:knowledge_admin:ops_admin",)).fetchone())
    assert row["actor"] == accounts["ops"]["id"]
    assert NEW_PASSWORD not in row["action"]


def test_password_reset_revokes_every_session(roles):
    client, store, accounts = roles
    _, first = login(client, "patient")
    client.cookies.clear()
    _, second = login(client, "patient")
    _, knowledge = login(client, "knowadmin")
    missing = client.post("/api/v1/admin/accounts/missing-user/password", headers=cookie(knowledge),
                          json={"password": NEW_PASSWORD})
    assert missing.status_code == 404 and missing.json()["code"] == "ACCOUNT_NOT_FOUND"
    assert NEW_PASSWORD not in missing.text
    short = client.post("/api/v1/admin/accounts/patient/password", headers=cookie(knowledge),
                        json={"password": "short-pass"})
    assert short.status_code == 422 and NEW_PASSWORD not in short.text and "short-pass" not in short.text
    reset = client.post("/api/v1/admin/accounts/patient/password", headers=cookie(knowledge),
                        json={"password": NEW_PASSWORD})
    assert reset.status_code == 200 and reset.json() == {"ok": True}
    assert NEW_PASSWORD not in reset.text
    assert client.get("/api/v1/auth/me", headers=cookie(first)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=cookie(second)).status_code == 401
    client.cookies.clear()
    assert client.post("/api/v1/auth/login", headers=cookie(knowledge),
                       json={"username": "patient", "password": PASSWORD}).status_code == 401
    fresh = client.post("/api/v1/auth/login", headers={"X-HealthPilot-Request": "1"},
                        json={"username": "patient", "password": NEW_PASSWORD})
    assert fresh.status_code == 200
    with store.connection() as execute:
        row = dict(execute("SELECT actor, action, resource FROM hp_audit WHERE action = 'user.password_reset' AND resource = ?",
                           (accounts["patient"]["id"],)).fetchone())
    assert row["actor"] == accounts["knowledge"]["id"]
    assert NEW_PASSWORD not in "".join(row.values())


def test_regular_user_and_demo_cannot_provision(roles, tmp_path, monkeypatch):
    client, _, _ = roles
    _, patient = login(client, "patient")
    denied = client.post("/api/v1/admin/accounts", headers=cookie(patient),
                         json={"username": "sneaky", "password": NEW_PASSWORD, "role": "ops_admin"})
    assert denied.status_code == 403 and denied.json()["code"] == "ACCESS_DENIED"
    assert NEW_PASSWORD not in denied.text
    bearer = client.post("/api/admin/accounts", headers={"Authorization": f"Bearer {ADMIN}"},
                         json={"username": "shared-entry", "password": NEW_PASSWORD, "role": "user"})
    assert bearer.status_code == 201, bearer.text
    assert NEW_PASSWORD not in bearer.text

    monkeypatch.setenv("HEALTH_AGENT_MOCK", "1")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "demo")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "demo-auth.sqlite3"))
    monkeypatch.setenv("HEALTH_AGENT_COOKIE_SECURE", "0")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    from app.main import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(), base_url="https://testserver") as demo:
        response = demo.post("/api/v1/admin/accounts", headers={"X-HealthPilot-Request": "1"},
                             json={"username": "demo-user", "password": NEW_PASSWORD, "role": "user"})
    assert response.status_code == 409 and response.json()["code"] == "AUTH_DEMO_MODE"
    assert NEW_PASSWORD not in response.text
