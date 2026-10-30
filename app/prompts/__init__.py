"""版本化提示词读取。"""
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str


def load_prompt(name: str) -> Prompt:
    if name not in {"advise", "answer", "extract", "intent"}:
        raise ValueError("未知提示词")
    raw = Path(__file__).with_name(name + ".md").read_text(encoding="utf-8")
    header, text = raw.split("\n", 1)
    return Prompt(text.strip(), re.fullmatch(r"<!-- version: ([\d.]+) -->", header)[1])
