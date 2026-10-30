"""中文自由文本的确定性问诊解析。"""
import re

REQUIRED_FIELDS = ["sex", "age", "sbp", "total_chol", "hdl", "smoker", "diabetes", "bp_treated"]
FIELD_LABELS = dict(sex="性别", age="年龄", sbp="血压", dbp="舒张压", total_chol="总胆固醇", hdl="高密度脂蛋白（HDL）", smoker="是否吸烟", diabetes="是否有糖尿病", bp_treated="是否在服降压药", height_cm="身高", weight_kg="体重")


def parse_profile(text: str) -> dict:
    result = {}
    if re.search(r"男(?:性|士)?", text):
        result["sex"] = "male"
    elif re.search(r"女(?:性|士)?", text):
        result["sex"] = "female"
    patterns = {
        "age": r"(?:今年\s*(\d{1,3})|(\d{1,3})\s*岁)",
        "height_cm": r"(?:身高\s*(\d+(?:\.\d+)?)\s*(厘米|cm|米)?|(\d+(?:\.\d+)?)\s*(cm|厘米|米))",
        "weight_kg": r"(?:体重\s*(\d+(?:\.\d+)?)\s*(kg|公斤|千克|斤)?|(\d+(?:\.\d+)?)\s*(kg|公斤|千克|斤))",
        "total_chol": r"(?:总胆固醇|\bTC)\s*[:：]?\s*(\d+(?:\.\d+)?)",
        "hdl": r"(?:\bHDL(?:-C)?|高密度脂蛋白(?:胆固醇)?|高密度|好胆固醇)\s*[:：]?\s*(\d+(?:\.\d+)?)",
        "sbp": r"(?:高压|收缩压)\s*[:：]?\s*(\d{2,3})",
        "dbp": r"(?:低压|舒张压)\s*[:：]?\s*(\d{2,3})",
    }
    for field, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        value = float(groups[0])
        if field == "height_cm" and len(groups) > 1 and groups[1] == "米":
            value = round(value * 100, 2)
        if field == "weight_kg" and len(groups) > 1 and groups[1] == "斤":
            value /= 2
        result[field] = int(value) if field in ("age", "sbp", "dbp") else value
    pressure = re.search(r"(?:血压\s*[:：]?\s*)?(\d{2,3})\s*[/／]\s*(\d{2,3})", text)
    if pressure:
        result.update(sbp=int(pressure[1]), dbp=int(pressure[2]))
    for field, negative, positive in [
        ("smoker", r"(?:不|没|无|从不|从未)(?:吸烟|抽烟)|戒烟了|已戒烟", r"吸烟|抽烟"),
        ("diabetes", r"(?:没有|无|不是|没|否认)(?:患有|患)?糖尿病", r"糖尿病"),
        ("bp_treated", r"(?:没|未|不|没有)(?:吃|服用|服|使用|在吃)降压药", r"(?:在吃|吃|服用|服|使用)降压药"),
    ]:
        if re.search(negative, text):
            result[field] = False
        elif re.search(positive, text):
            result[field] = True
    return result


def missing_fields(profile: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if profile.get(field) is None]
