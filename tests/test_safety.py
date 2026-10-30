import pytest

from app.safety import check_red_flags


@pytest.mark.parametrize(
    "text, flag",
    [
        ("突然胸口剧痛，出冷汗", "chest_pain"),
        ("胸痛半小时了", "chest_pain"),
        ("心口疼得厉害", "chest_pain"),
        ("胸口有压榨感", "chest_pain"),
        ("喘不上气", "breathing"),
        ("呼吸困难", "breathing"),
        ("我爸口角歪斜，说话不清", "stroke"),
        ("左边半边身体发麻无力", "stroke"),
        ("刚才晕倒了", "syncope"),
        ("失去意识几秒钟", "syncope"),
        ("不想活了", "self_harm"),
    ],
)
def test_red_flags_hit(text, flag):
    assert flag in check_red_flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "男，55岁，血压140/90，想评估一下风险",
        "没有胸痛，也不喘",
        "无胸痛",
        "我该怎么减盐？",
        "最近工作压力大，睡眠不好",
    ],
)
def test_red_flags_miss(text):
    assert check_red_flags(text) == []
