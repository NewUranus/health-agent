"""随访计划与回答分类的确定性规则。"""
import unicodedata
from datetime import date, timedelta

INTERVAL_DAYS = {"high": 30, "intermediate": 90, "low": 365}


def make_plan(level: str, today: date, symptoms: bool = False) -> dict:
    interval = INTERVAL_DAYS[level]
    if symptoms:
        interval = min(30, interval)
    return {"due_date": (today + timedelta(days=interval)).isoformat(), "interval_days": interval, "level": level}


def plan_status(plan: dict | None, today: date) -> dict:
    if not plan:
        return {"status": "none"}
    days = (date.fromisoformat(plan["due_date"]) - today).days
    return {"status": "upcoming" if days > 0 else "due" if days >= -14 else "overdue",
            "due_date": plan["due_date"], "days_until": days, "level": plan["level"]}


def classify_adherence(text: str) -> str:
    if any(word in text for word in ("没", "忘", "漏", "偶尔", "不太", "很少", "停")):
        return "poor"
    if any(word in text for word in ("按时", "都吃", "坚持", "做到", "不错", "挺好", "还行", "有")):
        return "good"
    return "unknown"


def reports_symptoms(text: str) -> bool:
    start, end = 0, len(text)
    while start < end and (text[start].isspace() or unicodedata.category(text[start]).startswith("P")):
        start += 1
    while end > start and (text[end - 1].isspace() or unicodedata.category(text[end - 1]).startswith("P")):
        end -= 1
    return not text[start:end].startswith(("没", "无", "都没", "还好", "正常", "不"))


def parse_smoking(text: str) -> bool:
    return not any(word in text for word in ("戒", "不", "没"))
