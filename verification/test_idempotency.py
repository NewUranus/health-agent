"""旧写接口幂等：带相同幂等键不重复执行，同键异参拒绝。"""
from test_foundation import headers, login, secured

ADMIN = {"Authorization": "Bearer local-admin-test"}
TEXT = {"title": "幂等导入", "source": "测试", "version": "v1", "text": "用于幂等导入的正文，确认重复请求不产生第二份草稿。"}


def test_text_import_and_approval_reuse_the_same_result(secured):
    client, _, _ = secured
    first = client.post("/api/v1/admin/text-documents", headers={**ADMIN, "Idempotency-Key": "text-import-key-01"}, json=TEXT)
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/admin/text-documents", headers={**ADMIN, "Idempotency-Key": "text-import-key-01"}, json=TEXT)
    assert second.status_code == 201 and second.json()["id"] == first.json()["id"]
    conflict = client.post("/api/v1/admin/text-documents", headers={**ADMIN, "Idempotency-Key": "text-import-key-01"},
                           json={**TEXT, "text": "另一段不同的正文，用来验证同键异参会被拒绝。"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    created = client.post("/api/v1/admin/approvals", headers={**ADMIN, "Idempotency-Key": "approval-key-01"},
                          json={"document_id": first.json()["id"], "action": "publish"})
    assert created.status_code == 201, created.text
    again = client.post("/api/v1/admin/approvals", headers={**ADMIN, "Idempotency-Key": "approval-key-01"},
                        json={"document_id": first.json()["id"], "action": "publish"})
    assert again.json()["id"] == created.json()["id"]
    listed = client.get("/api/v1/admin/approvals", headers=ADMIN).json()["approvals"]
    assert [row["id"] for row in listed].count(created.json()["id"]) == 1


def test_chat_idempotency_key_replays_one_task(secured):
    client, _, _ = secured
    _, token = login(client, "alice")
    request_headers = {**headers(token), "Idempotency-Key": "chat-idem-key-01"}
    body = {"thread_id": "idem-thread", "message": "你好"}
    first = client.post("/api/v1/chat", headers=request_headers, json=body)
    assert first.status_code == 200, first.text
    assert '"type": "done"' in first.text or '"type":"done"' in first.text
    second = client.post("/api/v1/chat", headers=request_headers, json=body)
    assert second.status_code == 200
    assert first.headers["x-task-id"] == second.headers["x-task-id"]
    conflict = client.post("/api/v1/chat", headers=request_headers, json={**body, "message": "另一句不同的话"})
    assert conflict.status_code == 409 and conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    tasks = client.get("/api/v1/tasks", headers=headers(token)).json()["tasks"]
    assert [task["id"] for task in tasks].count(first.headers["x-task-id"]) == 1
