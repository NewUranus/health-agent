import pymupdf
import pytest

from app.ingest import IngestError, chunk_pages, parse_pdf


def make_pdf(pages, **save):
    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((50, 80), text, fontname="china-s", fontsize=11)
    return doc.tobytes(**save)


def test_parse_pdf_keeps_pages_and_chinese():
    pages = parse_pdf(make_pdf(["第一章 总则\n高血压是常见的慢性病，需要长期管理。", "收缩压≥140 mmHg 可诊断为高血压，请遵医嘱。"]))
    assert [p["page"] for p in pages] == [1, 2]
    assert "高血压是常见的慢性病" in pages[0]["text"]
    assert "140" in pages[1]["text"]


def test_rejects_non_pdf():
    with pytest.raises(IngestError, match="不是有效的 PDF"):
        parse_pdf(b"hello world")


def test_rejects_scanned_pdf():
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    with pytest.raises(IngestError, match="扫描版"):
        parse_pdf(doc.tobytes())


def test_rejects_encrypted_pdf():
    data = make_pdf(["高血压是常见的慢性病，需要长期管理。"], encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    with pytest.raises(IngestError, match="已加密"):
        parse_pdf(data)


def page(n, text):
    return {"page": n, "text": text}


def test_chunk_splits_on_headings_and_tracks_page():
    chunks = chunk_pages([page(1, "第一章 总则\n高血压很常见。\n1.1 诊断\n收缩压140。"), page(2, "第二章 治疗\n先改生活方式。")])
    assert [c["heading"] for c in chunks] == ["第一章 总则", "1.1 诊断", "第二章 治疗"]
    assert [c["page"] for c in chunks] == [1, 1, 2]
    assert [c["seq"] for c in chunks] == [0, 1, 2]
    assert "收缩压140" in chunks[1]["text"]
    assert chunks[1]["text"].startswith("1.1 诊断")


def test_chunk_page_is_where_it_starts():
    chunks = chunk_pages([page(3, "二、 目标\n第一行内容。"), page(4, "接着上一页的内容。")])
    assert len(chunks) == 1
    assert chunks[0]["page"] == 3
    assert "接着上一页" in chunks[0]["text"]


def test_chunk_removes_repeated_header_and_page_numbers():
    pages = [page(i, f"某某指南 2020版\n第 {i} 页\n正文内容第{i}段。\n{i}") for i in range(1, 6)]
    text = "\n".join(c["text"] for c in chunk_pages(pages))
    assert "某某指南" not in text
    assert "第 3 页" not in text
    assert "正文内容第3段" in text


def test_header_rule_needs_three_pages():
    pages = [page(1, "重复行\n内容一。"), page(2, "重复行\n内容二。")]
    assert "重复行" in "\n".join(c["text"] for c in chunk_pages(pages))


def test_chunk_respects_max_chars_and_overlaps_long_lines():
    chunks = chunk_pages([page(1, "甲" * 200 + "乙" * 200 + "丙" * 200)], max_chars=250, overlap=30)
    assert len(chunks) >= 3
    assert all(len(c["text"]) <= 250 for c in chunks)
    assert chunks[0]["text"][-30:] == chunks[1]["text"][:30]
    joined = "".join(c["text"] for c in chunks)
    assert "丙" * 100 in joined


def test_chunk_accumulates_lines_until_limit():
    lines = "\n".join(f"第{i}句内容比较短。" for i in range(1, 41))
    chunks = chunk_pages([page(1, lines)], max_chars=100)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 100 for c in chunks)
    assert "第40句" in chunks[-1]["text"]


def test_blank_pages_give_no_chunks():
    assert chunk_pages([page(1, "   \n  ")]) == []


def test_long_line_is_not_treated_as_heading():
    long_line = "1 " + "很长的一句话" * 10
    chunks = chunk_pages([page(1, "第一章 总则\n" + long_line)])
    assert chunks[0]["heading"] == "第一章 总则"


@pytest.mark.parametrize(
    "line, expected",
    [
        ("第一章 总则", True),
        ("3.7\t Frequency of re-assessment", True),
        ("1.1 诊断标准", True),
        ("二、 治疗目标", True),
        ("40 mg once a day).", False),
        ("12.5 mg 每日一次。", False),
        ("2 次/日，", False),
        ("130/80 mmHg", False),
        ("3 Recommendations", True),
        ("25 mg or chlorthalidone", False),
        ("5 次每周", True),
    ],
)
def test_is_heading(line, expected):
    from app.ingest import is_heading

    assert is_heading(line) is expected


def test_dosage_line_does_not_start_a_new_chunk():
    chunks = chunk_pages([page(1, "3.4 Drug classes\nUse amlodipine 5 mg once a day.\n40 mg once a day).\nMore text here.")])
    assert [c["heading"] for c in chunks] == ["3.4 Drug classes"]
