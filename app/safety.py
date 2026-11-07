"""急症护栏：规则先判，规则未命中时再用模型补漏，先于问诊、工具和回答执行。

规则命中直接判急症，不经过模型。模型只能增加急症判断，不能撤销规则结果；
模型失败、超时或输出无效时按规则结果处理，并在事件里标出。
"""
import asyncio
import json
import re
from dataclasses import dataclass

PATTERNS = {
    "chest_pain": r"胸痛|胸口(?:突然)?(?:剧痛|疼痛|疼|有?压榨感|像压了块石头)|心口疼|胸骨后压榨",
    "breathing": r"喘不上气|呼吸困难|憋气",
    "stroke": r"口角歪斜|嘴歪|(?:一侧|半边|左侧|右侧)(?:身体)?[^，。；！？]{0,6}(?:无力|麻木|发麻)|说话不清|言语不清",
    "syncope": r"晕倒|昏迷|意识不清|失去意识",
    "self_harm": r"自杀|不想活|轻生",
}
TRIAGE_CATEGORIES = (*PATTERNS, "other")
# 分诊在每轮最前面执行，超时后按规则结果继续，不能让模型拖住急症指引
TRIAGE_TIMEOUT = 10
EVIDENCE_LIMIT = 200


def check_red_flags(text: str) -> list[str]:
    flags = []
    for category, pattern in PATTERNS.items():
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 12):match.start()]
            if not re.search(r"(?:没有|并无|否认|未出现|没有出现|无|不)(?:明显|任何|发生|出现|过)?$", prefix):
                flags.append(category)
                break
    return flags


@dataclass(frozen=True)
class Triage:
    """一次急症判定。source 为 rule / model / none；classifier 说明模型通道的状态。"""
    flags: tuple
    source: str
    classifier: str
    evidence: str = ""
    error_type: str = ""

    def event(self) -> dict:
        data = {"red_flags": list(self.flags), "source": self.source, "classifier": self.classifier}
        if self.evidence:
            data["evidence"] = self.evidence
        if self.classifier in ("failed", "invalid"):
            data["classifier_failed"] = True
        return data


def _compact(text: str) -> str:
    return "".join(text.split())


def parse_triage(raw: str, text: str):
    """解析模型输出。非急症返回 None，急症返回 (类别, 原文片段)；格式不合规抛 ValueError。"""
    if not isinstance(raw, str):
        raise ValueError("分诊输出不是文本")
    value = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
    if not isinstance(value, dict) or not isinstance(value.get("emergency"), bool):
        raise ValueError("分诊输出缺少 emergency 布尔值")
    if not value["emergency"]:
        return None
    evidence = value.get("evidence")
    # 依据必须逐字出自用户原文（忽略空白），防止模型凭空编造触发理由
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > EVIDENCE_LIMIT or _compact(evidence) not in _compact(text):
        raise ValueError("分诊依据不在用户原文中")
    category = value.get("category")
    return (category if category in TRIAGE_CATEGORIES else "other"), evidence.strip()


async def triage(text: str, classify=None, timeout: float = TRIAGE_TIMEOUT) -> Triage:
    """规则 + 模型双通道。classify 为 None（Mock 或未配置模型）时只走规则。"""
    flags = check_red_flags(text)
    if flags:
        return Triage(tuple(flags), "rule", "not_needed")
    if classify is None or not text.strip():
        return Triage((), "none", "skipped")
    try:
        raw = await asyncio.wait_for(classify(text), timeout)
    except Exception as exc:
        return Triage((), "none", "failed", error_type=type(exc).__name__)
    try:
        verdict = parse_triage(raw, text)
    except (ValueError, TypeError) as exc:
        return Triage((), "none", "invalid", error_type=type(exc).__name__)
    if verdict is None:
        return Triage((), "none", "ok")
    category, evidence = verdict
    return Triage((category,), "model", "ok", evidence)
