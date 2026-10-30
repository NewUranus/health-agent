import pytest

from app.tools_core import bmi, bp_category, framingham_cvd_risk


def test_framingham_matches_cvrisk_reference():
    # CVrisk ascvd_10y_frs(male, 55, hdl=50, totchol=213, sbp=140, 未治疗) = 13.53（mg/dL 换算成 mmol/L 输入）
    r = framingham_cvd_risk("male", 55, 213 / 38.67, 50 / 38.67, 140, False, False, False)
    assert r["risk_percent"] == pytest.approx(13.53, abs=0.01)
    assert r["level"] == "intermediate"


@pytest.mark.parametrize(
    "args, expected, level",
    [
        (("male", 55, 5.5, 1.3, 140, False, False, False), 13.45, "intermediate"),
        (("female", 60, 6.0, 1.2, 150, True, True, False), 28.52, "high"),
        (("male", 45, 4.5, 1.5, 118, False, False, False), 3.85, "low"),
        (("male", 65, 6.5, 0.9, 165, True, True, True), 93.2, "high"),
    ],
)
def test_framingham_vectors(args, expected, level):
    r = framingham_cvd_risk(*args)
    assert r["risk_percent"] == pytest.approx(expected, abs=0.01)
    assert r["level"] == level
    assert r["model"] == "Framingham 2008 general CVD"
    assert "China-PAR" in r["note"]


@pytest.mark.parametrize("age", [29, 75])
def test_framingham_rejects_age_out_of_range(age):
    with pytest.raises(ValueError):
        framingham_cvd_risk("male", age, 5.0, 1.2, 130, False, False, False)


def test_framingham_rejects_unknown_sex():
    with pytest.raises(ValueError):
        framingham_cvd_risk("x", 50, 5.0, 1.2, 130, False, False, False)


@pytest.mark.parametrize(
    "h, w, value, cat",
    [
        (172, 80, 27.0, "超重"),
        (160, 45, 17.6, "偏瘦"),
        (170, 65, 22.5, "正常"),
        (165, 80, 29.4, "肥胖"),
        (180, 77.7, 24.0, "超重"),
    ],
)
def test_bmi(h, w, value, cat):
    r = bmi(h, w)
    assert r["bmi"] == pytest.approx(value, abs=0.05)
    assert r["category"] == cat


@pytest.mark.parametrize(
    "sbp, dbp, cat",
    [
        (115, 75, "正常"),
        (125, 75, "正常高值"),
        (118, 85, "正常高值"),
        (140, 90, "1级高血压"),
        (135, 95, "1级高血压"),
        (165, 85, "2级高血压"),
        (150, 112, "3级高血压"),
        (185, 100, "3级高血压"),
    ],
)
def test_bp_category(sbp, dbp, cat):
    assert bp_category(sbp, dbp)["category"] == cat
