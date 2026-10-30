"""急症关键词护栏，先于模型和工具执行。"""
import re

PATTERNS = {
    "chest_pain": r"胸痛|胸口(?:突然)?(?:剧痛|疼痛|疼|有?压榨感|像压了块石头)|心口疼|胸骨后压榨",
    "breathing": r"喘不上气|呼吸困难|憋气",
    "stroke": r"口角歪斜|嘴歪|(?:一侧|半边|左侧|右侧)(?:身体)?[^，。；！？]{0,6}(?:无力|麻木|发麻)|说话不清|言语不清",
    "syncope": r"晕倒|昏迷|意识不清|失去意识",
    "self_harm": r"自杀|不想活|轻生",
}


def check_red_flags(text: str) -> list[str]:
    flags = []
    for category, pattern in PATTERNS.items():
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 12):match.start()]
            if not re.search(r"(?:没有|并无|否认|未出现|没有出现|无|不)(?:明显|任何|发生|出现|过)?$", prefix):
                flags.append(category)
                break
    return flags
