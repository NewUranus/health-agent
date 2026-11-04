"""使用部署方提供的美元单价估算用量费用，不替代供应商账单。"""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import os


def prices():
    try:
        table = json.loads(os.getenv("HEALTH_AGENT_MODEL_PRICES", "{}"))
        if not isinstance(table, dict):
            raise ValueError()
        for model, rates in table.items():
            if not isinstance(model, str) or not isinstance(rates, dict):
                raise ValueError()
            for key in ("input_per_million", "output_per_million"):
                rate = Decimal(str(rates[key]))
                if not rate.is_finite() or rate < 0 or rate > 1000000:
                    raise ValueError()
        return table
    except (ValueError, TypeError, KeyError, InvalidOperation):
        raise ValueError("HEALTH_AGENT_MODEL_PRICES 须为模型到输入/输出每百万 Token 美元价格的映射。") from None


def cost(record, table):
    rate = table.get(record["model"])
    if not rate or record["input_tokens"] is None or record["output_tokens"] is None:
        return None
    # 每百万 Token 的美元价乘 Token 数，数值正好为微美元。
    value = Decimal(str(rate["input_per_million"])) * record["input_tokens"] + Decimal(str(rate["output_per_million"])) * record["output_tokens"]
    return int(value.to_integral_value(rounding=ROUND_CEILING))
