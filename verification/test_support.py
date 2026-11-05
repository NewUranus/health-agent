"""运维协助队列：只看标记和账号，不返回健康正文。"""
import time
import uuid

from app.auth import digest
from test_roles import cookie, login, roles


def test_ops_can_list_and_resolve_support_without_health_text(roles):
    client, store, accounts = roles
    task_id, now = uuid.uuid4().hex, time.time_ns()
    payload = '{"thread_id":"support-thread","message":"胸痛细节不应出现在协助队列"}'
    with store.connection() as execute:
        execute("INSERT INTO hp_tasks (id, user_id, idempotency_key, payload_hash, thread_id, payload, status, created_at, updated_at, request_id, cancel_requested, error_code) VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?, 0, ?)",
                (task_id, accounts["patient"]["id"], "support-key-01", digest(payload), "support-thread", payload, now, now, "support-request", "TASK_EXECUTION_FAILED"))
        execute("INSERT INTO hp_task_reviews VALUES (?, 'needs_support', ?, ?)", (task_id, now, "support-request"))
    _, ops = login(client, "opsadmin")
    listed = client.get("/api/v1/admin/support", headers=cookie(ops))
    assert listed.status_code == 200, listed.text
    body = listed.text
    assert task_id in body and "patient" in body
    assert "胸痛" not in body and "message" not in body
    _, knowledge = login(client, "knowadmin")
    denied = client.get("/api/v1/admin/support", headers=cookie(knowledge))
    assert denied.status_code == 403 and denied.json()["code"] == "ADMIN_ROLE_DENIED"
    _, ops = login(client, "opsadmin")
    resolved = client.post(f"/api/v1/admin/support/{task_id}/resolve", headers=cookie(ops))
    assert resolved.status_code == 200, resolved.text
    again = client.get("/api/v1/admin/support", headers=cookie(ops)).json()["tasks"]
    assert task_id not in [row["id"] for row in again]
