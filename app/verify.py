"""引用范围、逐条引用与药物剂量的确定性校验。"""
import re
from dataclasses import dataclass


@dataclass
class VerifyResult:
    ok: bool
    problems: list[str]


def verify_advice(text: str, allowed_ids, per_item: bool = True) -> VerifyResult:
    """per_item=False 用于问答：工具算出的结论和过渡句不要求逐行带引用。"""
    problems = []
    citations = re.findall(r"\[cite:([^\]\s]+)\]", text)
    if not citations:
        problems.append("建议至少需要一个引用")
    for cid in dict.fromkeys(citations):
        if cid not in allowed_ids:
            problems.append(f"引用不在本轮检索范围内：{cid}")
    if re.search(r"\d+(?:\.\d+)?\s*(?:mg|毫克|μg|µg|微克)", text, re.I):
        problems.append("不得提供具体药物剂量")
    for item in re.split(r"\n|(?=\s-\s|\s\d+[.、]\s)", text) if per_item else []:
        item = item.strip()
        if item and not item.startswith(("#", ">")) and not re.search(r"\[cite:[^\]\s]+\]", item):
            problems.append("建议条目缺少引用：" + item[:60])
    return VerifyResult(not problems, problems)
