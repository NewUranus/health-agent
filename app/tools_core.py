"""确定性的健康计算；仅 MCP 服务端调用这些函数。"""
import math


def _positive(**values):
    for name, value in values.items():
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须为有限正数")


def framingham_cvd_risk(sex: str, age: int, total_chol_mmol: float, hdl_mmol: float,
                        sbp: int, bp_treated: bool, smoker: bool, diabetes: bool) -> dict:
    if sex not in ("male", "female"):
        raise ValueError("性别必须为 male 或 female")
    if not 30 <= age <= 74:
        raise ValueError("年龄超出模型适用范围 30–74 岁")
    _positive(age=age, total_chol_mmol=total_chol_mmol, hdl_mmol=hdl_mmol, sbp=sbp)
    if sex == "male":
        a, tc, hdl, bp, smoke, dm, mean, s0 = 3.06117, 1.12370, -0.93263, 1.99881 if bp_treated else 1.93303, .65451, .57367, 23.9802, .88936
    else:
        a, tc, hdl, bp, smoke, dm, mean, s0 = 2.32888, 1.20904, -.70833, 2.82263 if bp_treated else 2.76157, .52873, .69154, 26.1931, .95012
    linear = a * math.log(age) + tc * math.log(total_chol_mmol * 38.67) + hdl * math.log(hdl_mmol * 38.67) + bp * math.log(sbp) + smoke * smoker + dm * diabetes - mean
    risk = round(-math.expm1(math.log(s0) * math.exp(min(linear, 700))) * 100, 2)
    return {"risk_percent": risk, "level": "low" if risk < 10 else "intermediate" if risk < 20 else "high",
            "model": "Framingham 2008 general CVD", "note": "该模型基于美国人群，可能高估中国人群风险；国内推荐 China-PAR。"}


def bmi(height_cm: float, weight_kg: float) -> dict:
    _positive(height_cm=height_cm, weight_kg=weight_kg)
    value = round(weight_kg / (height_cm / 100) ** 2, 1)
    return {"bmi": value, "category": "偏瘦" if value < 18.5 else "正常" if value < 24 else "超重" if value < 28 else "肥胖"}


def bp_category(sbp: int, dbp: int) -> dict:
    _positive(sbp=sbp)
    if not math.isfinite(dbp) or dbp < 0:
        raise ValueError("舒张压必须为有限非负数")
    level = max(sum(sbp >= n for n in (120, 140, 160, 180)), sum(dbp >= n for n in (80, 90, 100, 110)))
    return {"category": ["正常", "正常高值", "1级高血压", "2级高血压", "3级高血压"][level]}


def simulate_intervention(sex: str, age: int, total_chol_mmol: float, hdl_mmol: float,
                          sbp: int, bp_treated: bool, smoker: bool, diabetes: bool,
                          quit_smoking: bool = False, sbp_target: int | None = None,
                          total_chol_target: float | None = None) -> dict:
    base = dict(sex=sex, age=age, total_chol_mmol=total_chol_mmol, hdl_mmol=hdl_mmol,
                sbp=sbp, bp_treated=bp_treated, smoker=smoker, diabetes=diabetes)
    baseline = framingham_cvd_risk(**base)["risk_percent"]
    changes = []
    if quit_smoking:
        base["smoker"] = False
        changes.append("戒烟")
    if sbp_target is not None:
        base["sbp"] = sbp_target
        changes.append(f"收缩压降至 {sbp_target} mmHg")
    if total_chol_target is not None:
        base["total_chol_mmol"] = total_chol_target
        changes.append(f"总胆固醇降至 {total_chol_target} mmol/L")
    simulated = framingham_cvd_risk(**base)["risk_percent"]
    reduction = round(baseline - simulated, 2)
    return {"baseline": baseline, "simulated": simulated, "absolute_reduction": reduction,
            "relative_reduction_percent": round(reduction / baseline * 100, 1) if baseline else 0.0,
            "changes": changes, "note": "这是基于风险公式的估算，不代表个体实际获益；目标需由医生结合个人情况确定。"}


def project_trajectory(sex: str, age: int, total_chol_mmol: float, hdl_mmol: float,
                       sbp: int, bp_treated: bool, smoker: bool, diabetes: bool,
                       years: list[int] | None = None) -> dict:
    base = dict(sex=sex, age=age, total_chol_mmol=total_chol_mmol, hdl_mmol=hdl_mmol,
                sbp=sbp, bp_treated=bp_treated, smoker=smoker, diabetes=diabetes)
    framingham_cvd_risk(**base)
    points = []
    for year in [0, 5, 10] if years is None else years:
        if isinstance(year, bool) or not isinstance(year, int) or year < 0:
            raise ValueError("推演年数必须为非负整数")
        if age + year <= 74:
            result = framingham_cvd_risk(**{**base, "age": age + year})
            points.append({"years": year, "age": age + year, "risk_percent": result["risk_percent"]})
    return {"points": points, "note": "假设其他因素不变，仅增加年龄；这是风险公式推演，不代表个体未来实际风险。"}
