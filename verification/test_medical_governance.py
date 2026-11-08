"""医学审核、指南核对元数据和 China-PAR 近似模型的独立验收。"""
import asyncio
from pathlib import Path
import sys

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ingest import parse_pdf
from app.knowledge import MemoryKnowledgeBase, KnowledgeError
from app.tools_core import china_par_risk_approx
from test_foundation import PASSWORD, headers, login, secured


def test_china_par_rounded_formula_is_explicitly_approximate():
    common = dict(total_chol_mmol=210 * 0.0259, hdl_mmol=55 * 0.0259, sbp=130,
                  bp_treated=False, smoker=False, diabetes=True, waist_cm=80,
                  north=True, urban=True, family_history=False)
    male = china_par_risk_approx(sex="male", age=40, **common)
    female = china_par_risk_approx(sex="female", age=40, **common)
    assert male["risk_percent"] == 2.4 and female["risk_percent"] == 2.74
    assert male["approximate"] is True and "可能相差约 2 个百分点" in male["note"]


def test_pdf_page_range_preserves_original_page_numbers():
    document = pymupdf.open()
    for page_number in range(1, 4):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {page_number} cardiovascular guidance text for validation.")
    data = document.tobytes()
    document.close()
    pages = parse_pdf(data, 2, 3)
    assert [page["page"] for page in pages] == [2, 3]
    assert "Page 2" in pages[0]["text"] and "Page 1" not in pages[0]["text"]


def test_knowledge_review_metadata_required_in_formal_mode():
    async def run():
        kb = MemoryKnowledgeBase()
        kb.require_review = True
        draft = await kb.create_draft("指南", "原始来源", "2026", [
            {"seq": 0, "page": 1, "heading": "建议", "text": "用于测试的完整医学建议文本。"}
        ], "guide.pdf")
        try:
            await kb.publication_vectors(draft)
        except KnowledgeError as exc:
            assert "审核人" in str(exc)
        else:
            raise AssertionError("未审核资料不应通过发布校验")
        reviewed = await kb.update_review_metadata(draft["id"], "张医生", "已核对数字", "2026-01-01")
        assert reviewed["reviewed_by"] == "张医生" and reviewed["reviewed_at"]
        await kb.publication_vectors(reviewed)
    asyncio.run(run())


def test_medical_review_queue_role_decision_and_user_notice(secured):
    client, store, users = secured
    reviewer = store.provision("reviewdoctor", PASSWORD, role="medical_reviewer")
    task_store = client.app.state.task_store
    task, _ = task_store.submit(users["alice"]["id"], "medical-review-task-01", {
        "thread_id": "medical-review-thread", "message": "请评估我的风险",
    }, "medical-review-request")
    job = task_store.claim("medical-review-worker")
    assert job and job["id"] == task["id"]
    task_store.append(task["id"], job["worker_token"], {
        "type": "node", "node": "risk_assess", "data": {"tool_calls": [{
            "tool": "cvd_risk", "result": {"risk_percent": 23.4, "level": "high"},
        }]},
    })
    task_store.append(task["id"], job["worker_token"], {"type": "message", "content": "测试高风险回复"})
    task_store.finish(task["id"], job["worker_token"], "succeeded")

    _, reviewer_token = login(client, "reviewdoctor")
    review_headers = headers(reviewer_token)
    listed = client.get("/api/v1/admin/medical-reviews", headers=review_headers)
    assert listed.status_code == 200, listed.text
    review = listed.json()["reviews"][0]
    assert review["task_id"] == task["id"] and review["reasons"] == ["high_risk"]
    assert client.get("/api/v1/admin/documents", headers=review_headers).status_code == 403
    decided = client.post(f'/api/v1/admin/medical-reviews/{review["id"]}/decision', headers=review_headers,
                          json={"decision": "approve", "note": "建议按计划尽快就诊。"})
    assert decided.status_code == 200 and decided.json()["status"] == "approved"

    _, user_token = login(client, "alice")
    notices = client.get("/api/v1/review-notices", headers=headers(user_token))
    assert notices.status_code == 200
    assert notices.json()["reviews"][0]["doctor_note"] == "建议按计划尽快就诊。"
    with store.connection() as execute:
        audit = execute("SELECT action FROM hp_audit WHERE actor = ?", (reviewer["id"],)).fetchall()
    assert any(row["action"] == "medical_review.approved" for row in audit)


def test_china_par_tool_is_exposed_through_mcp(secured):
    client, _, _ = secured
    _, token = login(client, "alice")
    response = client.post("/api/v1/tools/china_par_risk_approx", headers=headers(token), json={
        "sex": "male", "age": 40, "total_chol_mmol": 210 * 0.0259,
        "hdl_mmol": 55 * 0.0259, "sbp": 130, "bp_treated": False,
        "smoker": False, "diabetes": True, "waist_cm": 80,
        "north": True, "urban": True, "family_history": False,
    })
    assert response.status_code == 200, response.text
    assert response.json()["risk_percent"] == 2.4 and response.json()["approximate"] is True
