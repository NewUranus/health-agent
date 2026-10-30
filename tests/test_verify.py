from app.verify import verify_advice

ALLOWED = ["salt_reduction", "physical_activity", "smoking_cessation"]


def test_ok():
    text = "## 干预建议\n- 每天食盐不超过5 g [cite:salt_reduction]\n- 每周150分钟中等强度运动，血压目标<140 mmHg [cite:physical_activity]\n"
    r = verify_advice(text, ALLOWED)
    assert r.ok, r.problems
    assert r.problems == []


def test_no_citation():
    r = verify_advice("- 多运动\n- 少吃盐", ALLOWED)
    assert not r.ok
    assert r.problems


def test_unknown_citation():
    r = verify_advice("- 多吃深海鱼 [cite:made_up]", ALLOWED)
    assert not r.ok
    assert any("made_up" in p for p in r.problems)


def test_dosage_forbidden():
    for dose in ["100 mg", "100mg", "100毫克", "50 μg", "50微克", "5MG"]:
        r = verify_advice(f"- 阿司匹林每天{dose} [cite:salt_reduction]", ALLOWED)
        assert not r.ok, dose


def test_every_item_needs_citation():
    r = verify_advice("- 戒烟 [cite:smoking_cessation]\n- 每天快走半小时", ALLOWED)
    assert not r.ok


def test_numbered_items_and_blank_lines():
    text = "1. 戒烟 [cite:smoking_cessation]\n\n2. 限盐 [cite:salt_reduction]"
    assert verify_advice(text, ALLOWED).ok
