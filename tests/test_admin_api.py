"""后台接口：Mock 模式没有向量，走 BM25，验证权限和完整流程。"""

import re
import uuid

import pymupdf
import pytest

from conftest import chat, node_data, reply

AUTH = {"Authorization": "Bearer test-admin"}


def make_pdf(*pages):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((50, 80), text, fontname="china-s", fontsize=11)
    return doc.tobytes()


GUIDE = make_pdf("第一章 蓝莓与血压\n每天吃蓝莓一百克有助于心血管健康。", "第二章 散步\n饭后散步三十分钟有助于控制体重。")


def upload(client, title=None, data=GUIDE, filename="guide.pdf", **fields):
    title = title or "测试指南" + uuid.uuid4().hex[:6]
    return client.post("/api/admin/documents", headers=AUTH, files={"file": (filename, data, "application/pdf")}, data={"title": title, **fields})


@pytest.fixture
def draft(client):
    r = upload(client, source="《测试》", version="2020")
    assert r.status_code == 201, r.text
    yield r.json()
    client.delete(f"/api/admin/documents/{r.json()['id']}", headers=AUTH)


def test_admin_page_is_served(client):
    r = client.get("/admin")
    assert r.status_code == 200 and "<html" in r.text.lower()
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/admin/documents"),
        ("get", "/api/admin/documents/1"),
        ("post", "/api/admin/documents"),
        ("post", "/api/admin/documents/1/publish"),
        ("delete", "/api/admin/documents/1"),
        ("patch", "/api/admin/documents/1/chunks/x"),
        ("delete", "/api/admin/documents/1/chunks/x"),
        ("get", "/api/admin/stats"),
        ("get", "/api/admin/chunks"),
        ("get", "/api/admin/search?q=x"),
    ],
)
def test_every_admin_route_needs_password(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_admin_disabled_without_password(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD")
    r = client.get("/api/admin/documents", headers=AUTH)
    assert r.status_code == 503 and r.json()["detail"] == "后台未启用。"


def test_public_routes_stay_public(client):
    assert client.get("/api/health").status_code == 200


def test_upload_makes_a_draft_with_pages(draft):
    assert draft["status"] == "draft" and draft["source"] == "《测试》" and draft["version"] == "2020" and draft["filename"] == "guide.pdf"
    chunks = draft["chunks"]
    assert [c["page"] for c in chunks] == [1, 2]
    assert chunks[0]["heading"] == "第一章 蓝莓与血压"
    assert all(re.fullmatch(r"[\w-]+", c["id"]) for c in chunks)


def test_upload_errors(client):
    bad = upload(client, data=b"not a pdf")
    assert bad.status_code == 400 and "不是有效的 PDF" in bad.json()["detail"]
    blank = pymupdf.open()
    blank.new_page()
    scanned = upload(client, data=blank.tobytes())
    assert scanned.status_code == 400 and "扫描版" in scanned.json()["detail"]
    too_big = upload(client, data=b"%PDF" + b"0" * (20 * 1024 * 1024 + 1))
    assert too_big.status_code == 413
    no_title = client.post("/api/admin/documents", headers=AUTH, files={"file": ("a.pdf", GUIDE, "application/pdf")})
    assert no_title.status_code in (400, 422)


def test_full_flow_draft_edit_publish_search_chat_delete(client, draft):
    doc_id = draft["id"]
    assert client.get("/api/admin/search", headers=AUTH, params={"q": "蓝莓"}).json()["results"] == []

    first, second = draft["chunks"]
    r = client.patch(f"/api/admin/documents/{doc_id}/chunks/{first['id']}", headers=AUTH, json={"text": first["text"] + "（已核对）"})
    assert r.status_code == 200
    assert client.patch(f"/api/admin/documents/{doc_id}/chunks/{first['id']}", headers=AUTH, json={"text": "  "}).status_code == 400
    assert client.delete(f"/api/admin/documents/{doc_id}/chunks/{second['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/api/admin/documents/{doc_id}", headers=AUTH).json()["chunk_count"] == 1

    published = client.post(f"/api/admin/documents/{doc_id}/publish", headers=AUTH)
    assert published.status_code == 200 and published.json()["status"] == "published"
    assert client.patch(f"/api/admin/documents/{doc_id}/chunks/{first['id']}", headers=AUTH, json={"text": "改"}).status_code == 409
    assert client.delete(f"/api/admin/documents/{doc_id}/chunks/{first['id']}", headers=AUTH).status_code == 409

    results = client.get("/api/admin/search", headers=AUTH, params={"q": "蓝莓"}).json()["results"]
    assert results[0]["id"] == first["id"] and results[0]["page"] == 1 and "已核对" in results[0]["text"]

    listed = client.get("/api/admin/chunks", headers=AUTH, params={"document_id": doc_id}).json()
    assert listed["total"] == 1 and listed["chunks"][0]["id"] == first["id"]
    assert client.get("/api/admin/chunks", headers=AUTH, params={"q": "已核对"}).json()["total"] == 1
    stats = client.get("/api/admin/stats", headers=AUTH).json()
    assert stats["backend"] == "memory" and stats["published_documents"] >= 17  # 16 段内置 + 这份

    # 发布后问答真的能引用到
    tid = uuid.uuid4().hex
    chat(client, tid, "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药")
    events = chat(client, tid, "吃蓝莓有什么好处")
    answer = node_data(events, "answer")
    assert answer["chunks"][0]["id"] == first["id"] and answer["chunks"][0]["page"] == 1
    assert f"[cite:{first['id']}]" in reply(events)

    assert client.delete(f"/api/admin/documents/{doc_id}", headers=AUTH).status_code == 200
    assert client.get(f"/api/admin/documents/{doc_id}", headers=AUTH).status_code == 404
    assert client.get("/api/admin/search", headers=AUTH, params={"q": "蓝莓"}).json()["results"] == [] or \
        client.get("/api/admin/search", headers=AUTH, params={"q": "蓝莓"}).json()["results"][0]["id"] != first["id"]


def test_builtin_documents_are_listed_and_protected(client):
    docs = client.get("/api/admin/documents", headers=AUTH).json()["documents"]
    builtin = [d for d in docs if d["builtin"]]
    assert len(builtin) >= 16
    assert all(d["status"] == "published" for d in builtin)
    assert client.delete(f"/api/admin/documents/{builtin[0]['id']}", headers=AUTH).status_code == 409
    salt = client.get("/api/admin/search", headers=AUTH, params={"q": "减盐"}).json()["results"]
    assert salt[0]["id"] == "salt_reduction"


def test_chunks_limit_is_capped(client):
    assert client.get("/api/admin/chunks", headers=AUTH, params={"limit": 1000}).status_code in (200, 400, 422)
    r = client.get("/api/admin/chunks", headers=AUTH, params={"limit": 3})
    assert len(r.json()["chunks"]) == 3 and r.json()["total"] >= 16


def test_missing_document_is_404(client):
    assert client.get("/api/admin/documents/999999", headers=AUTH).status_code == 404
    assert client.post("/api/admin/documents/999999/publish", headers=AUTH).status_code == 404
    assert client.delete("/api/admin/documents/999999", headers=AUTH).status_code == 404
