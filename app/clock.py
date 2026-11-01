"""统一使用上海时区的业务日期，便于测试替换。"""
from datetime import date, datetime
from zoneinfo import ZoneInfo


def today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
