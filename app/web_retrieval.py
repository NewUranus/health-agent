"""协调 MCP 搜索与正文读取，网络内容只作为本轮临时证据。"""
import asyncio
import re

from app.web_tools import search_terms

WEB_TOOLS = {"search_web", "read_web_document"}


def document_chunks(document, query):
    terms = search_terms(re.sub(r"https?://\S+", "", query))
    chunks = []
    sequence = 0
    for page in document["pages"]:
        text = page["text"]
        for start in range(0, len(text), 900):
            passage = text[start:start + 1000].strip()
            if not passage:
                continue
            sequence += 1
            score = sum(term in passage.casefold() for term in terms) / max(1, len(terms))
            chunks.append({"id": f"web-{document['document_id']}-{sequence}", "title": document["title"],
                           "source": document["url"], "url": document["url"], "text": passage,
                           "page": page["page"], "score": score, "origin": "web",
                           "fetched_at": document["fetched_at"], "reviewed": False})
    return sorted(chunks, key=lambda chunk: chunk["score"], reverse=True)[:3]


async def retrieve_web(mcp, text):
    data = {"chunks": [], "tool_calls": [], "web_results": [], "web_errors": [], "web_search": True}

    async def call(name, args):
        try:
            trace = await mcp.call_with_trace(name, args)
        except Exception:
            trace = {"tool": name, "args": args, "result": {"error": "网页工具暂不可用或超时。"}, "via": "mcp", "cached": False}
        output = trace["result"]
        # 原文只放在筛选后的 chunks 中，不重复传输完整网页或写入日志。
        data["tool_calls"].append({**trace, "result": {k: v for k, v in output.items() if k != "pages"}})
        if output.get("error"):
            data["web_errors"].append(output["error"])
        return output

    urls = list(dict.fromkeys(url.rstrip("。，；、！？.,;!）)]}>\"'”)】")
                              for url in re.findall(r"https?://[^\s<>]+", text)))[:3]
    if urls:
        data["web_results"] = [{"title": "用户提供的链接", "url": url} for url in urls]
    else:
        searched = await call("search_web", {"query": text[:500], "max_results": 5})
        data["web_results"] = searched.get("results", [])
        urls = [item["url"] for item in data["web_results"][:3]]
    documents = await asyncio.gather(*(call("read_web_document", {"url": url}) for url in urls))
    seen = set()
    for document in documents:
        if document.get("pages") and document["url"] not in seen:
            seen.add(document["url"])
            data["chunks"].extend(document_chunks(document, text))
    data["chunks"] = sorted(data["chunks"], key=lambda chunk: chunk["score"], reverse=True)[:6]
    data["web_read_count"] = len(seen)
    data["web_failed"] = not bool(data["chunks"])
    return data
