"""PDF 文字解析与保留页码的确定性切块。"""
import re
from collections import Counter

import pymupdf


class IngestError(ValueError):
    """可直接展示给上传者的解析错误。"""


def parse_pdf(data: bytes) -> list[dict]:
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise IngestError("文件不是有效的 PDF。") from None
    with document:
        if not document.is_pdf:
            raise IngestError("文件不是有效的 PDF。")
        if document.is_encrypted or (document.metadata or {}).get("encryption"):
            raise IngestError("PDF 已加密，请先解除密码。")
        if document.page_count > 500:
            raise IngestError("PDF 超过 500 页，请拆分后上传。")
        try:
            pages = [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(document)]
        except Exception:
            raise IngestError("文件不是有效的 PDF。") from None
        if not pages or sum(len(re.sub(r"\s", "", p["text"])) for p in pages) < 20 * len(pages):
            raise IngestError("没有提取到文字，可能是扫描版，目前不支持。")
        return pages


# 编号后必须是大写字母或汉字：排除"25 mg or chlorthalidone"这类剂量行
HEADING = re.compile(r"^第.+[章节]|^\d+(\.\d+)*\s+[A-Z\u4e00-\u9fff]|^[一二三四五六七八九十]+、")


def is_heading(line: str) -> bool:
    """以句末标点结尾的是正文（如"40 mg once a day)."），不是标题。"""
    return len(line) <= 40 and bool(HEADING.match(line)) and not re.search(r"[。.；;，,:：)）]$", line)


def clean_pages(pages: list[dict]) -> list[dict]:
    """切块前恢复常见排版断行；保留页码、数字、标题和列表边界。"""
    # 仅合并能确认的断词，其余连字符保留，避免把 low-density 等复合词改错。
    joined_words = {
        "lifestyle", "hypertension", "nonpharmacological", "pharmacological",
        "cardiovascular", "recommendation", "recommendations", "treatment",
        "intervention", "interventions", "modification", "modifications", "prevention",
    }
    counts = Counter(line for page in pages for line in {line.strip() for line in page['text'].splitlines()} if line)
    noise = {line for line, count in counts.items() if len(pages) >= 3 and count > len(pages) / 2}
    cleaned = []
    for page in pages:
        lines = page['text'].replace('\u00a0', ' ').splitlines()
        output = []
        pending_bullet = False
        for original in lines:
            line = original.strip()
            if re.fullmatch(r"[\[•●▪◦]+", line):
                pending_bullet = True
                continue
            if line in noise or re.fullmatch(r"\d+|第\s*\d+\s*页|-\s*\d+\s*-", line):
                continue
            if not line:
                output.append('')
                continue
            # 不改写 [1]、[95% CI] 等引用或统计表达式。
            line = re.sub(r"^(?:\[\s+(?=[A-Za-z\u4e00-\u9fff])|[•●▪◦]\s*)", '- ', line)
            if pending_bullet and not line.startswith('- '):
                line = '- ' + line
            pending_bullet = False
            boundary = (is_heading(line) or line.startswith('- ') or
                        bool(re.match(r"\d|[一二三四五六七八九十]+[、.]", line)) or
                        bool(re.search(r"\t| {2,}", original.strip())))
            previous = output[-1] if output else ''
            if previous and not boundary and not is_heading(previous) and not re.search(r"[。！？.!?;；:：]$", previous):
                # 英文小写续行较明确；中文与表格不猜测合段。
                if re.match(r"[a-z]", line) and re.search(r"[A-Za-z][\-\u00ad]?$", previous):
                    tail = re.search(r"([A-Za-z]+)([\-\u00ad])$", previous)
                    head = re.match(r"[a-z]+", line)
                    if tail and head:
                        word = (tail[1] + head[0]).lower()
                        output[-1] = previous[:-1] + line if tail[2] == '\u00ad' or word in joined_words else previous + line
                    else:
                        output[-1] = previous + ' ' + line
                    continue
            output.append(line)
        cleaned.append({**page, 'text': '\n'.join(output)})
    return cleaned


def chunk_pages(pages: list[dict], max_chars: int = 500, overlap: int = 50) -> list[dict]:
    if max_chars <= 0 or not 0 <= overlap < max_chars:
        raise IngestError("切块长度必须为正数，重叠长度必须小于切块长度。")
    lines = [[line.strip() for line in page["text"].splitlines() if line.strip()] for page in pages]
    counts = Counter(line for page in lines for line in set(page))
    noise = {line for line, count in counts.items() if len(pages) >= 3 and count > len(pages) / 2}
    chunks, buffer = [], ""
    heading, first_page = "", None

    def flush():
        nonlocal buffer
        if buffer.strip():
            chunks.append({"seq": len(chunks), "page": first_page, "heading": heading, "text": buffer})
        buffer = ""

    for page, page_lines in zip(pages, lines):
        for line in page_lines:
            if line in noise or re.fullmatch(r"\d+|第\s*\d+\s*页|-\s*\d+\s*-", line):
                continue
            if is_heading(line):
                flush()
                heading = line
            if buffer and len(buffer) + 1 + len(line) > max_chars:
                flush()
            if not buffer:
                first_page = page["page"]
            while len(line) > max_chars:
                buffer = line[:max_chars]
                flush()
                line = line[max_chars - overlap:]
            buffer = buffer + "\n" + line if buffer else line
    flush()
    return chunks
