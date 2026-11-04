"""三角色登录与后台双通道鉴权验收；不修改仓库原有 tests/。"""
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import COOKIE_NAME, role_allows_admin

PASSWORD = "local-role-validation-password"
WRITE = {"X-HealthPilot-Request": "1"}
ADMIN = "local-admin-role-test"


@pytest.fixture
def roles(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_AGENT_MOCK", "1")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_MODE", "required")
    monkeypatch.setenv("HEALTH_AGENT_AUTH_DB", str(tmp_path / "auth.sqlite3"))
    monkeypatch.setenv("HEALTH_AGENT_COOKIE_SECURE", "0")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN)
    from app.main import create_app
    app = create_app()
    with TestClient(app, base_url="https://testserver") as client:
        store = app.state.auth_store
        accounts = {
            "patient": store.provision("patient", PASSWORD, role="user"),
            "knowledge": store.provision("knowadmin", PASSWORD, role="knowledge_admin"),
            "ops": store.provision("opsadmin", PASSWORD, role="ops_admin"),
        }
        yield client, store, accounts


def login(client, username):
    response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD}, headers=WRITE)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["role"] in {"user", "knowledge_admin", "ops_admin"}
    return body, client.cookies.get(COOKIE_NAME)


def cookie(token):
    return {**WRITE, "Cookie": f"{COOKIE_NAME}={token}"}


def test_login_returns_role_and_me(roles):
    client, _, accounts = roles
    body, token = login(client, "patient")
    assert body["user"]["role"] == "user"
    assert body["user"]["id"] == accounts["patient"]["id"]
    me = client.get("/api/v1/auth/me", headers=cookie(token))
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "user"
    body, _ = login(client, "knowadmin")
    assert body["user"]["role"] == "knowledge_admin"
    body, _ = login(client, "opsadmin")
    assert body["user"]["role"] == "ops_admin"


def test_knowledge_role_can_read_docs_not_ops(roles):
    client, _, _ = roles
    _, token = login(client, "knowadmin")
    headers = cookie(token)
    docs = client.get("/api/v1/admin/documents", headers=headers)
    assert docs.status_code == 200, docs.text
    ops = client.get("/api/v1/admin/operations", headers=headers)
    assert ops.status_code == 403
    assert ops.json()["code"] == "ADMIN_ROLE_DENIED"


def test_ops_role_can_read_ops_not_docs(roles):
    client, _, _ = roles
    _, token = login(client, "opsadmin")
    headers = cookie(token)
    ops = client.get("/api/v1/admin/operations", headers=headers)
    assert ops.status_code == 200, ops.text
    docs = client.get("/api/v1/admin/documents", headers=headers)
    assert docs.status_code == 403
    assert docs.json()["code"] == "ADMIN_ROLE_DENIED"


def test_user_cannot_access_admin(roles):
    client, _, _ = roles
    _, token = login(client, "patient")
    response = client.get("/api/v1/admin/documents", headers=cookie(token))
    assert response.status_code == 403
    assert response.json()["code"] == "ACCESS_DENIED"


def test_bearer_password_still_opens_all_admin(roles):
    client, _, _ = roles
    auth = {"Authorization": f"Bearer {ADMIN}"}
    assert client.get("/api/v1/admin/documents", headers=auth).status_code == 200
    assert client.get("/api/v1/admin/operations", headers=auth).status_code == 200


def test_role_allows_admin_helper():
    assert role_allows_admin("knowledge_admin", "/admin/documents")
    assert role_allows_admin("knowledge_admin", "/admin/approvals")
    assert not role_allows_admin("knowledge_admin", "/admin/operations")
    assert role_allows_admin("ops_admin", "/admin/logs")
    assert role_allows_admin("ops_admin", "/admin/queue")
    assert not role_allows_admin("ops_admin", "/admin/chunks")
    assert not role_allows_admin("user", "/admin/documents")
