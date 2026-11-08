"""医学审核队列：确定性识别高风险结果，审核决定由独立角色完成。"""
import json
import time
import uuid

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthFailure


REVIEW_REASONS = {
    "high_risk": "心血管风险为高风险",
    "grounding_removed": "回答删除了缺少依据的断言",
    "model_emergency": "急症由模型补充识别",
}


def _events(execute, task_id):
    rows = execute("SELECT payload FROM hp_task_events WHERE task_id = ? ORDER BY seq", (task_id,)).fetchall()
    return [json.loads(row["payload"]) for row in rows]


def review_snapshot(execute, task):
    """只依据持久事件入队，不让模型自行决定是否需要医学审核。"""
    reasons, risk_level, risk_percent, reply = [], "", None, ""
    for event in _events(execute, task["id"]):
        if event.get("type") == "message":
            reply = event.get("content", "")
        if event.get("type") != "node":
            continue
        data = event.get("data") or {}
        if event.get("node") in {"safety_check", "checkin"} and data.get("source") == "model" and data.get("red_flags"):
            reasons.append("model_emergency")
        grounding = data.get("grounding") or {}
        if isinstance(grounding, dict) and grounding.get("removed", 0) > 0:
            reasons.append("grounding_removed")
        if event.get("node") == "risk_assess":
            for call in data.get("tool_calls", []):
                if call.get("tool") not in {"cvd_risk", "china_par_risk_approx"} or not isinstance(call.get("result"), dict):
                    continue
                result = call["result"]
                risk_level, risk_percent = str(result.get("level", "")), result.get("risk_percent")
                if risk_level == "high":
                    reasons.append("high_risk")
    reasons = list(dict.fromkeys(reasons))
    if not reasons or not reply:
        return None
    payload = json.loads(task["payload"])
    return {
        "question": str(payload.get("message", "")), "reply": reply,
        "risk_level": risk_level, "risk_percent": risk_percent,
        "reasons": reasons,
    }


def enqueue_review(execute, task):
    snapshot = review_snapshot(execute, task)
    if not snapshot:
        return None
    ident, created_at = uuid.uuid4().hex, time.time_ns()
    execute("""INSERT INTO hp_medical_reviews
        (id, task_id, user_id, thread_id, question, reply, risk_level, risk_percent, reasons,
         status, reviewer_id, doctor_note, created_at, reviewed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, NULL)
        ON CONFLICT (task_id) DO NOTHING""",
        (ident, task["id"], task["user_id"], task["thread_id"], snapshot["question"], snapshot["reply"],
         snapshot["risk_level"], snapshot["risk_percent"], json.dumps(snapshot["reasons"], ensure_ascii=False), created_at))
    return ident


class MedicalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern=r"^(approve|request_changes)$")
    note: str = Field(default="", max_length=4000)


class MedicalReviewStore:
    def __init__(self, database):
        self.db = database

    def setup(self):
        with self.db.connection() as execute:
            execute("""CREATE TABLE IF NOT EXISTS hp_medical_reviews (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES hp_tasks(id),
                user_id TEXT NOT NULL REFERENCES hp_users(id), thread_id TEXT NOT NULL,
                question TEXT NOT NULL, reply TEXT NOT NULL, risk_level TEXT NOT NULL,
                risk_percent REAL, reasons TEXT NOT NULL, status TEXT NOT NULL,
                reviewer_id TEXT, doctor_note TEXT NOT NULL, created_at BIGINT NOT NULL, reviewed_at BIGINT)""")
            execute("CREATE INDEX IF NOT EXISTS hp_medical_reviews_status ON hp_medical_reviews(status, created_at)")
            execute("CREATE INDEX IF NOT EXISTS hp_medical_reviews_user ON hp_medical_reviews(user_id, created_at)")

    @staticmethod
    def public(row, include_content=True):
        value = {key: row[key] for key in (
            "id", "task_id", "thread_id", "risk_level", "risk_percent", "status", "reviewer_id", "doctor_note")}
        value.update(reasons=json.loads(row["reasons"]),
                     created_at=row["created_at"] // 1_000_000,
                     reviewed_at=row["reviewed_at"] // 1_000_000 if row["reviewed_at"] else None)
        if include_content:
            value.update(question=row["question"], reply=row["reply"])
        return value

    def list_admin(self, status="pending", limit=100):
        if status not in {"pending", "approved", "changes_requested", "all"}:
            raise AuthFailure(422, "INVALID_REVIEW_STATUS", "医学审核状态无效。")
        where = "" if status == "all" else " WHERE r.status = ?"
        args = () if status == "all" else (status,)
        with self.db.connection() as execute:
            rows = execute("""SELECT r.*, u.username FROM hp_medical_reviews r
                JOIN hp_users u ON u.id = r.user_id""" + where + " ORDER BY r.created_at LIMIT ?", (*args, limit)).fetchall()
        return [{**self.public(row), "username": row["username"],
                 "reason_labels": [REVIEW_REASONS.get(reason, reason) for reason in json.loads(row["reasons"])]}
                for row in rows]

    def decide(self, review_id, reviewer_id, decision, note, request_id):
        status = "approved" if decision == "approve" else "changes_requested"
        with self.db.connection() as execute:
            suffix = " FOR UPDATE" if self.db.url else ""
            row = execute("SELECT * FROM hp_medical_reviews WHERE id = ?" + suffix, (review_id,)).fetchone()
            if not row:
                raise AuthFailure(404, "MEDICAL_REVIEW_NOT_FOUND", "医学审核项不存在。")
            if row["status"] != "pending":
                if row["status"] == status and row["reviewer_id"] == reviewer_id and row["doctor_note"] == note:
                    return self.public(row)
                raise AuthFailure(409, "MEDICAL_REVIEW_DECIDED", "该审核项已经处理。")
            now = time.time_ns()
            execute("UPDATE hp_medical_reviews SET status = ?, reviewer_id = ?, doctor_note = ?, reviewed_at = ? WHERE id = ?",
                    (status, reviewer_id, note.strip(), now, review_id))
            self.db._audit(execute, reviewer_id, "medical_review." + status, review_id, request_id, 200)
            row = execute("SELECT * FROM hp_medical_reviews WHERE id = ?", (review_id,)).fetchone()
        return self.public(row)

    def notices(self, user_id, limit=20):
        with self.db.connection() as execute:
            rows = execute("""SELECT * FROM hp_medical_reviews
                WHERE user_id = ? AND status != 'pending' ORDER BY reviewed_at DESC LIMIT ?""",
                (user_id, limit)).fetchall()
        return [self.public(row) for row in rows]


def _medical_reviewer(request: Request):
    user = request.state.user
    if request.state.admin_auth != "session" or not user or user.get("role") != "medical_reviewer":
        raise AuthFailure(403, "MEDICAL_REVIEWER_REQUIRED", "请使用医学审核员账号。")
    if request.app.state.auth_store.current_role(user["id"]) != "medical_reviewer":
        raise AuthFailure(403, "MEDICAL_REVIEWER_REQUIRED", "医学审核权限已经变化，请重新登录。")
    return user["id"]


def attach_medical_review_api(router):
    @router.get("/admin/medical-reviews")
    async def medical_reviews(request: Request, status: str = "pending", limit: int = 100):
        _medical_reviewer(request)
        return {"reviews": request.app.state.medical_reviews.list_admin(status, min(200, max(1, limit)))}

    @router.post("/admin/medical-reviews/{review_id}/decision")
    async def decide_medical_review(request: Request, review_id: str, body: MedicalDecision):
        reviewer = _medical_reviewer(request)
        return request.app.state.medical_reviews.decide(
            review_id, reviewer, body.decision, body.note, request.state.request_id)

    @router.get("/review-notices")
    async def medical_review_notices(request: Request):
        user = request.state.user
        if not user:
            raise AuthFailure(401, "AUTH_REQUIRED", "请先登录。")
        return {"reviews": request.app.state.medical_reviews.notices(user["id"])}
