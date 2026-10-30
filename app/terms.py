"""用于检索的医学俗称归一。"""
import json
from pathlib import Path

TERMS = json.loads((Path(__file__).resolve().parent.parent / "data/terms.json").read_text(encoding="utf-8"))


def expand_query(text: str) -> str:
    expanded = list(dict.fromkeys(term for alias, terms in TERMS.items() if alias in text for term in terms))
    return " ".join([text, *expanded])
