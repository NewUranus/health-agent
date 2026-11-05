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


def test_knowledge_session_can_reject_approval_without_shared_password(roles, monkeypatch):
    client, _, _ = roles
    monkeypatch.delenv("ADMIN_PASSWORD")
    _, token = login(client, "knowadmin")
    document = client.portal.call(client.app.state.agent.kb.create_draft, "角色确认资料", "测试来源", "v1",
                                  [{"seq": 0, "page": 1, "heading": "测试", "text": "发布前核对。"}], "role.pdf")
    created = client.post("/api/v1/admin/approvals", headers=cookie(token),
                          json={"document_id": document["id"], "action": "publish"})
    assert created.status_code == 201, created.text
    approval = created.json()
    rejected = client.post(f'/api/v1/admin/approvals/{approval["id"]}/decision', headers=cookie(token),
                           json={"decision": "reject", "fingerprint": approval["fingerprint"]})
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    client.cookies.clear()
    blocked = client.get("/api/v1/admin/approvals", headers={"Authorization": "Bearer leftover"})
    assert blocked.status_code == 503 and blocked.json()["code"] == "ADMIN_DISABLED"


def test_role_change_revokes_session_and_is_audited(roles):
    client, store, accounts = roles
    _, token = login(client, "knowadmin")
    changed = store.assign_role("knowadmin", "user")
    assert changed["changed"] is True and changed["role"] == "user"
    assert client.get("/api/v1/auth/me", headers=cookie(token)).status_code == 401
    unchanged = store.assign_role("knowadmin", "user")
    assert unchanged["changed"] is False
    with store.connection() as execute:
        rows = execute("SELECT action, resource FROM hp_audit WHERE resource = ?", (accounts["knowledge"]["id"],)).fetchall()
    assert any(row["action"] == "user.role_changed:knowledge_admin:user" for row in rows)


def test_demotion_blocks_pending_approval(roles):
    client, store, _ = roles
    _, token = login(client, "knowadmin")
    document = client.portal.call(client.app.state.agent.kb.create_draft, "降权确认资料", "测试来源", "v1",
                                  [{"seq": 0, "page": 1, "heading": "测试", "text": "发布前核对。"}], "demote.pdf")
    created = client.post("/api/v1/admin/approvals", headers=cookie(token),
                          json={"document_id": document["id"], "action": "publish"})
    assert created.status_code == 201, created.text
    approval = created.json()
    with store.connection() as execute:
        execute("UPDATE hp_users SET role = 'user' WHERE username = 'knowadmin'")
    denied = client.post(f'/api/v1/admin/approvals/{approval["id"]}/decision', headers=cookie(token),
                         json={"decision": "approve", "fingerprint": approval["fingerprint"]})
    assert denied.status_code == 403
    assert denied.json()["code"] == "ACCESS_DENIED"


def test_role_allows_admin_helper():
    assert role_allows_admin("knowledge_admin", "/admin/documents")
    assert role_allows_admin("knowledge_admin", "/admin/approvals")
    assert not role_allows_admin("knowledge_admin", "/admin/operations")
    assert role_allows_admin("ops_admin", "/admin/logs")
    assert role_allows_admin("ops_admin", "/admin/support")
    assert not role_allows_admin("knowledge_admin", "/admin/support")
    assert role_allows_admin("ops_admin", "/admin/queue")
    assert not role_allows_admin("ops_admin", "/admin/chunks")
    assert not role_allows_admin("user", "/admin/documents")
