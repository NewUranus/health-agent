"""回答内容的事实支持校验：拆句、逐句判定、删除没有资料支持的句子。

判定交给模型，但只能依据该句引用的资料原文和确定性工具结果；
拆句、哪些句子参与判定、判定无效时怎么处理、删哪些句子，都是确定性代码。
"""
import json
import re
from dataclasses import dataclass, field

CITE = re.compile(r"\[cite:([^\]\s]+)\]")
LEADING_CITES = re.compile(r"^(\s*(?:\[cite:[^\]\s]+\]\s*)+)(.*)$", re.S)
BULLET = re.compile(r"^(\s*(?:[-*•]|\d+[.、)])\s+)")
SENTENCE_END = re.compile(r"(?<=[。！？!?；;])")
# 说明句：免责声明、资料覆盖范围说明、转介就医。不含数字时不参与校验
META = re.compile(
    r"仅为技术演示|不构成医疗建议|仅供参考"
    r"|(?:资料|指南|摘要|知识库|依据)[^。！？\n]{0,12}(?:未|没有|不足|无法|不涉及|未涉及|未覆盖|不包括|有限|针对|适用于|面向)"
    r"|无法回答|不能回答|无法判断"
    r"|(?:建议|请|应|尽快|及时)[^。！？\n]{0,4}(?:咨询|就诊|就医|找)(?:医生|专科医生|医院)?"
)
CONTENT = re.compile(r"[一-鿿A-Za-z0-9]")
REASON_LIMIT = 60


@dataclass(frozen=True)
class Claim:
    index: int
    line: int
    raw: str
    text: str
    cites: tuple
    meta: bool


@dataclass
class GroundingResult:
    checked: int = 0
    unsupported: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    skipped: str = ""

    def event(self, removed=0, rewritten=False, first=None):
        if self.skipped:
            return {"grounding_skipped": True, "grounding_skip_reason": self.skipped}
        data = {"checked": self.checked, "unsupported": [claim.text for claim in self.unsupported], "removed": removed, "rewritten": rewritten}
        if first is not None:
            # 重写前被判不支持的句子，便于分析判定是否过严
            data["initial_unsupported"] = [claim.text for claim in first.unsupported]
        return {"grounding": data}


def _plain(segment: str) -> str:
    text = CITE.sub("", segment)
    text = re.sub(r"\*\*|__|`", "", text)
    return re.sub(r"\s+(?=[。！？!?；;，,])", "", text).strip()


def _is_meta(text: str) -> bool:
    if not CONTENT.search(text):
        return True
    # “方法如下：”这类引出下文的句子本身不陈述事实，事实在冒号后面的句子里
    if text.endswith(("：", ":")):
        return True
    return bool(META.search(text)) and not re.search(r"\d", text)


def _lines(text: str):
    """按行拆出列表符号和句子片段；句末引用归到前一句。"""
    for number, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if not stripped:
            yield number, "", []
            continue
        if stripped.startswith(("#", ">", "|")):
            yield number, "", [(line, True)]
            continue
        marker = BULLET.match(line)
        prefix = marker[1] if marker else ""
        segments = []
        for part in SENTENCE_END.split(line[len(prefix):]):
            moved = LEADING_CITES.match(part)
            if moved and segments:
                segments[-1] = (segments[-1][0] + moved[1], None)
                part = moved[2]
            if part:
                segments.append((part, None))
        yield number, prefix, segments


def split_claims(text: str) -> list:
    claims = []
    for number, _, segments in _lines(text):
        line = []
        for raw, forced_meta in segments:
            plain = _plain(raw)
            if plain:
                line.append([raw, plain, tuple(dict.fromkeys(CITE.findall(raw))), bool(forced_meta) or _is_meta(plain)])
        # “A；B [cite:x]”里的 A 没有自己的引用，沿用同一行后面最近的引用
        following = ()
        for item in reversed(line):
            if item[2]:
                following = item[2]
            elif following:
                item[2] = following
        for raw, plain, cites, meta in line:
            claims.append(Claim(len(claims) + 1, number, raw, plain, cites, meta))
    return claims


def remove_claims(text: str, drop) -> str:
    """删除指定编号的句子，保留其余原文与列表符号；整行删空时连同列表符号一起删掉。"""
    drop = set(drop)
    output, index = [], 0
    for _, prefix, segments in _lines(text):
        if not segments:
            output.append("")
            continue
        kept = []
        for raw, _ in segments:
            if _plain(raw):
                index += 1
                if index in drop:
                    continue
            kept.append(raw)
        body = "".join(kept).strip()
        if body and CONTENT.search(_plain(body)):
            output.append(prefix + body)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
    return cleaned


VERDICT_PAIR = re.compile(r'"id"\s*:\s*(\d+)\s*,\s*"verdict"\s*:\s*"(supported|unsupported)"')


def parse_verdicts(raw) -> dict:
    """解析判定输出；没有结果的句子由调用方当作不支持。

    模型偶尔在 reason 里写未转义的引号，整段 JSON 解析失败时只按 id 与 verdict 成对提取，
    不能因为理由写坏就把所有句子都判成不支持。
    """
    if not isinstance(raw, str):
        return {}
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        value = json.loads(body)
    except ValueError:
        return {int(index): (verdict, "") for index, verdict in VERDICT_PAIR.findall(body)}
    items = value.get("results") if isinstance(value, dict) else value
    verdicts = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        verdict = item.get("verdict")
        if verdict in ("supported", "unsupported"):
            reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
            verdicts[item["id"]] = (verdict, reason[:REASON_LIMIT])
    return verdicts


async def check_grounding(claims, sources: dict, judge, tool_results=None, user_input=None) -> GroundingResult:
    """sources 为 {引用 id: 资料原文}；judge 接收判定载荷、返回模型原始输出，异常由调用方处理。

    没有引用的句子对照本轮全部资料、工具结果和用户自己提供的信息判断；
    引用了本轮之外的资料，或本轮没有任何可对照内容的句子，直接判不支持。
    """
    targets = [claim for claim in claims if not claim.meta]
    result = GroundingResult(checked=len(targets))
    if not targets:
        return result
    direct, judged = [], []
    for claim in targets:
        known = [cid for cid in claim.cites if cid in sources]
        if len(known) != len(claim.cites) or (not claim.cites and not sources and not tool_results):
            direct.append(claim)
        else:
            judged.append(claim)
    for claim in direct:
        result.reasons[claim.index] = "没有可对照的资料"
    verdicts = {}
    if judged:
        # 有无引用的句子时，它要对照全部资料，资料就全部发送
        used = sources if any(not claim.cites for claim in judged) else dict.fromkeys(cid for claim in judged for cid in claim.cites)
        payload = {"资料": {cid: sources[cid] for cid in used},
                   "工具结果": tool_results or None,
                   "用户提供的信息": user_input or None,
                   "句子": [{"id": claim.index, "内容": claim.text, "引用": list(claim.cites)} for claim in judged]}
        verdicts = parse_verdicts(await judge(payload))
    for claim in judged:
        verdict, reason = verdicts.get(claim.index, ("unsupported", "判定结果缺失或无效"))
        if verdict != "supported":
            direct.append(claim)
            result.reasons[claim.index] = reason
    result.unsupported = sorted(direct, key=lambda claim: claim.index)
    return result


def has_substance(claims) -> bool:
    return any(not claim.meta for claim in claims)
