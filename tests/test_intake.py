import pytest

from app.intake import REQUIRED_FIELDS, missing_fields, parse_profile


def test_full_sentence():
    p = parse_profile("男，55岁，身高172cm，体重80kg，血压140/90，总胆固醇5.5，HDL 1.3，吸烟，无糖尿病，未服降压药")
    assert p == {
        "sex": "male",
        "age": 55,
        "height_cm": 172,
        "weight_kg": 80,
        "sbp": 140,
        "dbp": 90,
        "total_chol": 5.5,
        "hdl": 1.3,
        "smoker": True,
        "diabetes": False,
        "bp_treated": False,
    }


@pytest.mark.parametrize(
    "text, expected",
    [
        ("我是女性，今年62", {"sex": "female", "age": 62}),
        ("女士，48岁", {"sex": "female", "age": 48}),
        ("高压150低压95", {"sbp": 150, "dbp": 95}),
        ("身高1.72米，体重160斤", {"height_cm": 172, "weight_kg": 80}),
        ("体重70公斤", {"weight_kg": 70}),
        ("高密度脂蛋白1.1，TC 6.2", {"hdl": 1.1, "total_chol": 6.2}),
        ("高密度1.0", {"hdl": 1.0}),
        ("有糖尿病，在吃降压药，抽烟", {"diabetes": True, "bp_treated": True, "smoker": True}),
    ],
)
def test_partial(text, expected):
    p = parse_profile(text)
    for k, v in expected.items():
        assert p.get(k) == pytest.approx(v), f"{k}: {p}"


@pytest.mark.parametrize(
    "text, field",
    [
        ("不吸烟", "smoker"),
        ("不抽烟", "smoker"),
        ("戒烟了", "smoker"),
        ("从不抽烟", "smoker"),
        ("没有糖尿病", "diabetes"),
        ("不是糖尿病", "diabetes"),
        ("没吃降压药", "bp_treated"),
        ("不吃降压药", "bp_treated"),
    ],
)
def test_negation_wins(text, field):
    assert parse_profile(text)[field] is False


def test_unrelated_text_gives_nothing():
    assert parse_profile("我想了解一下心血管健康") == {}


def test_missing_fields_keeps_order():
    assert REQUIRED_FIELDS == ["sex", "age", "sbp", "total_chol", "hdl", "smoker", "diabetes", "bp_treated"]
    assert missing_fields({"sex": "male", "age": 55, "smoker": False}) == [
        "sbp",
        "total_chol",
        "hdl",
        "diabetes",
        "bp_treated",
    ]
    assert missing_fields(parse_profile("男，55岁，血压140/90，总胆固醇5.5，HDL 1.3，不吸烟，没有糖尿病，没吃降压药")) == []
