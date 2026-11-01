"""随访流程：到期日期、多轮 interrupt 问诊、中途急症、取消、到期状态接口。"""

import json
import uuid
from datetime import date

import pytest

from app.followup import classify_adherence, make_plan, parse_smoking, plan_status, reports_symptoms
from conftest import node_data, nodes, reply

FULL = "男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药"
SMOKER = FULL.replace("不吸烟", "抽烟")
TREATED = FULL.replace("没吃降压药", "在吃降压药")
TODAY = date(2026, 11, 1)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.clock.today", lambda: TODAY)


def uid():
    return uuid.uuid4().hex


def post(client, thread, user, message):
    events = []
    with client.stream("POST", "/api/chat", json={"thread_id": thread, "user_id": user, "message": message}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    assert events[-1] == {"type": "done"}
    assert not [e for e in events if e["type"] == "error"], events
    return events


def status(client, user):
    r = client.get("/api/followup", params={"user_id": user})
    assert r.status_code == 200
    return r.json()


def question(events):
    return node_data(events, "checkin_question")


# ---------- 纯函数 ----------


@pytest.mark.parametrize(
    "level, days, due", [("high", 30, "2026-12-01"), ("intermediate", 90, "2027-01-30"), ("low", 365, "2027-11-01")]
)
def test_make_plan(level, days, due):
    assert make_plan(level, TODAY) == {"due_date": due, "interval_days": days, "level": level}


def test_make_plan_symptoms_shorten_interval():
    assert make_plan("low", TODAY, symptoms=True)["interval_days"] == 30
    assert make_plan("high", TODAY, symptoms=True)["interval_days"] == 30


@pytest.mark.parametrize(
    "today, expected, days",
    [
        (date(2027, 1, 29), "upcoming", 1),
        (date(2027, 1, 30), "due", 0),
        (date(2027, 2, 13), "due", -14),
        (date(2027, 2, 14), "overdue", -15),
    ],
)
def test_plan_status(today, expected, days):
    s = plan_status({"due_date": "2027-01-30", "interval_days": 90, "level": "intermediate"}, today)
    assert s == {"status": expected, "due_date": "2027-01-30", "days_until": days, "level": "intermediate"}


def test_plan_status_none():
    assert plan_status(None, TODAY) == {"status": "none"}


@pytest.mark.parametrize(
    "text, expected",
    [("按时吃", "good"), ("都挺好", "good"), ("偶尔忘记", "poor"), ("没吃", "poor"), ("停了一周", "poor"), ("嗯嗯", "unknown")],
)
def test_classify_adherence(text, expected):
    assert classify_adherence(text) == expected


@pytest.mark.parametrize(
    "text, expected", [("没有", False), ("无", False), ("都没有。", False), ("还好", False), ("有点头晕", True), ("胸闷", True)]
)
def test_reports_symptoms(text, expected):
    assert reports_symptoms(text) is expected


@pytest.mark.parametrize("text, expected", [("戒了", False), ("不抽了", False), ("还在抽", True), ("是的", True)])
def test_parse_smoking(text, expected):
    assert parse_smoking(text) is expected


# ---------- 首次评估写入计划 ----------


def test_assessment_sets_due_date(client):
    user = uid()
    assert status(client, user) == {"status": "none"}
    events = post(client, uid(), user, FULL)
    assert node_data(events, "followup")["due_date"] == "2027-01-30"
    assert "下次复查日期：2027-01-30" in reply(events)
    s = status(client, user)
    assert s["status"] == "upcoming" and s["days_until"] == 90 and s["level"] == "intermediate"
    assert s["last_risk_percent"] == pytest.approx(13.45, abs=0.01)


def test_status_follows_the_clock(client, monkeypatch):
    user = uid()
    post(client, uid(), user, FULL)
    monkeypatch.setattr("app.clock.today", lambda: date(2027, 1, 30))
    assert status(client, user)["status"] == "due"
    monkeypatch.setattr("app.clock.today", lambda: date(2027, 3, 1))
    s = status(client, user)
    assert s["status"] == "overdue" and s["days_until"] == -30


# ---------- 多轮问诊 ----------


def test_full_questionnaire_across_threads(client):
    user = uid()
    post(client, uid(), user, FULL)
    thread = uid()

    first = post(client, thread, user, "我来复查，最近血压130/85")
    assert node_data(first, "supervisor")["route"] == "checkin_agent"
    assert question(first) == {"key": "adherence", "step": 1, "total": 2}
    assert [e["agent"] for e in first if e.get("node") == "checkin_question"] == ["checkin_agent"]
    assert "限盐" in reply(first)
    assert "checkin" not in nodes(first)

    second = post(client, thread, user, "都挺好")
    assert nodes(second) == ["checkin_question"]  # 恢复执行，不重走前面的节点
    assert question(second) == {"key": "symptoms", "step": 2, "total": 2}
    assert "不适" in reply(second)

    third = post(client, thread, user, "没有")
    assert nodes(third) == ["checkin"]
    data = node_data(third, "checkin")
    assert data["previous_risk"] == pytest.approx(13.45, abs=0.01)
    assert data["current_risk"] == pytest.approx(11.77, abs=0.01)
    assert data["delta"] == pytest.approx(-1.68, abs=0.01)
    assert data["adherence"] == "good" and data["symptoms"] is False and data["bp_updated"] is True
    assert data["due_date"] == "2027-01-30"
    assert data["tool_calls"][0]["via"] == "mcp"
    text = reply(third)
    assert "上次风险：13.5%" in text
    assert "本次风险：11.8%" in text
    assert "历次风险：13.5% → 11.8%" in text
    assert "依从情况：良好" in text
    assert "不适症状：无" in text
    assert "下次复查日期：2027-01-30" in text

    # 新会话能看到这次记录，历次风险继续累积
    again = uid()
    post(client, again, user, "我来复查，最近血压120/80")
    post(client, again, user, "还行")
    last = post(client, again, user, "没有")
    assert node_data(last, "checkin")["previous_risk"] == pytest.approx(11.77, abs=0.01)
    assert "历次风险：13.5% → 11.8% → 10.2%" in reply(last)


def test_asks_for_bp_when_missing(client):
    user = uid()
    post(client, uid(), user, FULL)
    thread = uid()
    first = post(client, thread, user, "我来复查")
    assert question(first) == {"key": "bp", "step": 1, "total": 3}
    assert "血压" in reply(first)
    second = post(client, thread, user, "130/85")
    assert question(second)["key"] == "adherence" and question(second)["step"] == 2
    post(client, thread, user, "都挺好")
    done = post(client, thread, user, "没有")
    assert node_data(done, "checkin")["current_risk"] == pytest.approx(11.77, abs=0.01)


def test_bp_unparsable_retries_then_uses_old_value(client):
    user = uid()
    post(client, uid(), user, FULL)
    thread = uid()
    post(client, thread, user, "我来复查")
    retry = post(client, thread, user, "不记得了")
    assert question(retry)["key"] == "bp" and question(retry)["step"] == 1
    assert "没有识别出血压" in reply(retry)
    next_q = post(client, thread, user, "真不记得")
    assert question(next_q)["key"] == "adherence"
    post(client, thread, user, "都挺好")
    done = post(client, thread, user, "没有")
    data = node_data(done, "checkin")
    assert data["bp_updated"] is False
    assert data["current_risk"] == pytest.approx(13.45, abs=0.01)
    assert "血压没有更新" in reply(done)


def test_smoker_is_asked_and_profile_updates(client):
    user = uid()
    post(client, uid(), user, SMOKER)
    thread = uid()
    first = post(client, thread, user, "我来复查，最近血压130/85")
    assert question(first)["total"] == 3
    second = post(client, thread, user, "都挺好")
    assert question(second)["key"] == "smoking"
    assert "吸烟" in reply(second)
    third = post(client, thread, user, "戒了")
    assert question(third)["key"] == "symptoms"
    done = post(client, thread, user, "没有")
    assert node_data(done, "checkin")["current_risk"] == pytest.approx(11.77, abs=0.01)
    assert node_data(done, "checkin")["profile"]["smoker"] is False  # 前端靠它刷新档案卡
    later = post(client, uid(), user, "我来复查，最近血压130/85")
    assert question(later)["total"] == 2  # 档案已更新，不再问吸烟


def test_still_smoking_high_risk(client):
    user = uid()
    post(client, uid(), user, SMOKER)
    thread = uid()
    post(client, thread, user, "我来复查，最近血压130/85")
    post(client, thread, user, "都挺好")
    post(client, thread, user, "还在抽")
    done = post(client, thread, user, "没有")
    data = node_data(done, "checkin")
    assert data["current_risk"] == pytest.approx(21.4, abs=0.05)
    assert "仍属高风险" in reply(done)
    assert data["due_date"] == "2026-12-01"


def test_treated_user_gets_medication_question_and_warning(client):
    user = uid()
    post(client, uid(), user, TREATED)
    thread = uid()
    first = post(client, thread, user, "我来复查，最近血压130/85")
    assert "降压药" in reply(first)
    post(client, thread, user, "偶尔忘记")
    done = post(client, thread, user, "没有")
    assert node_data(done, "checkin")["adherence"] == "poor"
    text = reply(done)
    assert "依从情况：需要改进" in text
    assert "不要自行停药" in text


def test_symptoms_shorten_followup(client):
    user = uid()
    post(client, uid(), user, FULL)
    thread = uid()
    post(client, thread, user, "我来复查，最近血压130/85")
    post(client, thread, user, "都挺好")
    done = post(client, thread, user, "有点头晕")
    data = node_data(done, "checkin")
    assert data["symptoms"] is True and data["due_date"] == "2026-12-01"
    assert "不适症状：有" in reply(done)
    assert "尽快就诊" in reply(done)
    assert status(client, user)["due_date"] == "2026-12-01"


# ---------- 中途安全 ----------


@pytest.mark.parametrize("answer_index", [0, 1])
def test_red_flag_during_questionnaire_stops_everything(client, answer_index):
    user = uid()
    post(client, uid(), user, FULL)
    before = status(client, user)
    thread = uid()
    post(client, thread, user, "我来复查，最近血压130/85")
    if answer_index == 1:
        post(client, thread, user, "都挺好")
    events = post(client, thread, user, "突然胸口剧痛，出冷汗")
    assert "120" in reply(events)
    data = node_data(events, "checkin")
    assert data["red_flags"]
    assert "tool_calls" not in data or data["tool_calls"] == []
    assert status(client, user) == before

    # 问诊已结束，下一条不再被当成回答
    after = post(client, thread, user, "我该怎么减盐？")
    assert node_data(after, "supervisor")["route"] == "qa_agent"


def test_cancel_changes_nothing(client):
    user = uid()
    post(client, uid(), user, FULL)
    before = status(client, user)
    thread = uid()
    post(client, thread, user, "我来复查，最近血压130/85")
    events = post(client, thread, user, "取消")
    assert "已取消" in reply(events)
    assert node_data(events, "checkin")["cancelled"] is True
    assert status(client, user) == before
    after = post(client, uid(), user, "我来复查，最近血压130/85")
    assert question(after)["total"] == 2
    assert status(client, user) == before


def test_red_flag_in_trigger_message_goes_to_emergency(client):
    user = uid()
    post(client, uid(), user, FULL)
    events = post(client, uid(), user, "我来复查，现在胸痛")
    assert nodes(events) == ["safety_check", "emergency"]


def test_questionnaire_is_per_thread(client):
    user = uid()
    post(client, uid(), user, FULL)
    thread = uid()
    post(client, thread, user, "我来复查，最近血压130/85")
    other = post(client, uid(), user, "我该怎么减盐？")
    assert node_data(other, "supervisor")["route"] == "qa_agent"
    resumed = post(client, thread, user, "都挺好")
    assert question(resumed)["key"] == "symptoms"
