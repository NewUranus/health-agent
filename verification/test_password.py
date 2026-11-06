"""登录后自行修改密码：注销其他会话，审计不保存密码。"""
from test_roles import PASSWORD, WRITE, cookie, login, roles

NEW_PASSWORD = "replacement-password-2026"


def test_password_change_keeps_current_session_and_drops_others(roles):
    client, store, accounts = roles
    _, first = login(client, "patient")
    client.cookies.clear()
    _, current = login(client, "patient")
    wrong = client.post("/api/v1/auth/password", headers=cookie(current),
                        json={"current_password": "not-the-password", "new_password": NEW_PASSWORD})
    assert wrong.status_code == 401 and wrong.json()["code"] == "AUTH_INVALID_CREDENTIALS"
    assert NEW_PASSWORD not in wrong.text and "not-the-password" not in wrong.text
    same = client.post("/api/v1/auth/password", headers=cookie(current),
                       json={"current_password": PASSWORD, "new_password": PASSWORD})
    assert same.status_code == 422 and same.json()["code"] == "PASSWORD_UNCHANGED"
    short = client.post("/api/v1/auth/password", headers=cookie(current),
                        json={"current_password": PASSWORD, "new_password": "short-pass"})
    assert short.status_code == 422
    changed = client.post("/api/v1/auth/password", headers=cookie(current),
                          json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200, changed.text
    assert NEW_PASSWORD not in changed.text
    assert client.get("/api/v1/auth/me", headers=cookie(first)).status_code == 401
    assert client.get("/api/v1/auth/me", headers=cookie(current)).status_code == 200
    client.cookies.clear()
    old = client.post("/api/v1/auth/login", json={"username": "patient", "password": PASSWORD}, headers=WRITE)
    assert old.status_code == 401
    fresh = client.post("/api/v1/auth/login", json={"username": "patient", "password": NEW_PASSWORD}, headers=WRITE)
    assert fresh.status_code == 200 and fresh.json()["user"]["role"] == "user"
    with store.connection() as execute:
        row = dict(execute("SELECT action, resource FROM hp_audit WHERE action = 'user.password_changed'").fetchone())
    assert row["resource"] == accounts["patient"]["id"]
    assert NEW_PASSWORD not in row["action"] and NEW_PASSWORD not in row["resource"]
