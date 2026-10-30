import pytest

from app.rag import Retriever
from conftest import ROOT


@pytest.fixture(scope="module")
def retriever():
    return Retriever.from_dir(ROOT / "corpus")


def test_loads_all_chunks_with_metadata(retriever):
    hits = retriever.search("戒烟", k=1)
    c = hits[0]
    assert c.id == "smoking_cessation"
    assert c.title == "戒烟"
    assert "《" in c.source
    assert "任何年龄戒烟都能获益" in c.text
    assert c.score > 0


@pytest.mark.parametrize(
    "query, top",
    [
        ("我该怎么减盐？", "salt_reduction"),
        ("胸痛 大汗 心肌梗死", "chest_pain_emergency"),
        ("运动 每周多少分钟", "physical_activity"),
        ("总胆固醇 偏高", "lipids"),
        ("口角歪斜 说话不清", "stroke_recognition"),
        ("喝酒", "alcohol"),
        ("降压目标", "bp_targets"),
        ("BMI 超重标准", "bmi_weight"),
    ],
)
def test_top1(retriever, query, top):
    assert retriever.search(query, k=3)[0].id == top


def test_k_and_order(retriever):
    hits = retriever.search("血压", k=4)
    assert len(hits) == 4
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_no_match_returns_empty(retriever):
    assert retriever.search("xyzzy", k=4) == []
