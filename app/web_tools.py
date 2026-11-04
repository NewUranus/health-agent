"""无需密钥的公开网页搜索与正文读取；只允许访问公网 HTTP(S)。"""
import hashlib
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

MAX_BYTES = 8 * 1024 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HealthPilot/1.0)", "Accept-Encoding": "identity"}
SEARCH_TERMS = {
    "高血压": "hypertension", "血压": "blood pressure", "胆固醇": "cholesterol", "血脂": "lipid",
    "心血管": "cardiovascular", "糖尿病": "diabetes", "冠心病": "coronary heart disease",
    "心力衰竭": "heart failure", "心衰": "heart failure", "脑卒中": "stroke", "中风": "stroke",
    "吸烟": "smoking", "戒烟": "smoking cessation", "运动": "exercise", "饮食": "diet",
    "膳食": "diet", "肥胖": "obesity", "减重": "weight loss", "指南": "guideline",
}
SEARCH_STOPWORDS = {"帮我", "搜索", "查询", "查找", "资料", "如何", "怎么", "什么", "哪些", "可以", "应该", "一下", "请问", "关于", "最新", "指南", "成人"}


def search_terms(query):
    """按完整词过滤明显无关结果，并补充常见主题的英文检索词。"""
    import jieba
    jieba.setLogLevel(logging.WARNING)
    words = {word.casefold() for word in jieba.lcut(query) if len(word.strip()) > 1 and word not in SEARCH_STOPWORDS}
    for chinese, english in SEARCH_TERMS.items():
        if chinese in query and chinese not in SEARCH_STOPWORDS:
            words.add(english)
    return words


class WebError(ValueError):
    """可直接展示的网页访问失败原因。"""


def public_url(url):
    """每次跳转都检查目标，并返回用于连接的已校验 IP。"""
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\x00-\x20\x7f]", url):
        raise WebError("网址格式无效。")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme not in ("http", "https") or not host or parts.username is not None or parts.password is not None:
            raise ValueError()
        if port != (443 if parts.scheme == "https" else 80):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise WebError("仅支持标准端口的公开 HTTP 或 HTTPS 网址。") from None
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise WebError("无法解析网页域名。") from None
    ips = list(dict.fromkeys(row[4][0] for row in addresses))
    addresses = [ipaddress.ip_address(ip) for ip in ips]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise WebError("不能访问本机、内网或保留地址。")
    return parts, host, port, ips[0]


def fetch_public(url, max_bytes=MAX_BYTES):
    """固定连接已检查的 IP，TLS 仍验证原域名，防止 DNS 重绑定。"""
    deadline = time.monotonic() + 18
    for _ in range(4):
        parts, host, port, address = public_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WebError("网页读取超时。")
        timeout = min(5, remaining)
        if parts.scheme == "https":
            connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        # 不在建立连接时再次解析域名；Host 请求头和 TLS 的域名保持原值。
        connection._create_connection = lambda target, timeout, source_address=None: socket.create_connection(
            (address, port), timeout, source_address)
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="%=&?/:@!$'()*+,;~-._")
        target = path + ("?" + query if query else "")
        try:
            connection.request("GET", target, headers=HEADERS)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise WebError("网页跳转缺少目标地址。")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise WebError("网站拒绝访问、需要登录或暂不可用。")
            length = response.getheader("Content-Length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise WebError("网页或文件超过大小限制。")
            if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
                raise WebError("暂不支持该网页的压缩传输格式。")
            data = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WebError("网页读取超时。")
                if connection.sock:
                    connection.sock.settimeout(min(5, remaining))
                block = response.read1(min(65536, max_bytes + 1 - len(data)))
                if not block:
                    break
                data.extend(block)
                if len(data) > max_bytes:
                    raise WebError("网页或文件超过大小限制。")
            return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")), response.getheader("Content-Type", ""), bytes(data)
        except (OSError, http.client.HTTPException):
            raise WebError("网页连接失败或超时。") from None
        finally:
            connection.close()
    raise WebError("网页跳转次数过多。")


def decode_page(data, content_type):
    declaration = content_type + " " + data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", declaration, re.I)
    encoding = match[1] if match else "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


class PageParser(HTMLParser):
    """去除脚本和导航，并保留正文段落及搜索结果链接。"""
    ignored = {"script", "style", "noscript", "svg", "nav", "footer", "form", "template"}
    blocks = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3", "h4", "tr", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts, self.title, self.links = [], [], []
        self.in_title = False
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.ignored:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        if tag in self.blocks:
            self.parts.append("\n")
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            self.anchor = {"url": attrs.get("href", ""), "title": ""}

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "title":
            self.in_title = False
        if tag in self.blocks:
            self.parts.append("\n")
        if tag == "a" and self.anchor is not None:
            self.links.append(self.anchor)
            self.anchor = None

    def handle_data(self, data):
        if self.hidden:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.parts.append(data)
        if self.anchor is not None:
            self.anchor["title"] += data

    def text(self):
        return "\n".join(line for raw in "".join(self.parts).splitlines() if (line := re.sub(r"\s+", " ", raw).strip()))


def search_web(query: str, max_results: int = 5) -> dict:
    """从公开搜索页面获取链接；不使用 API Key，不绕过验证码。"""
    query = query.strip()
    if not query or len(query) > 500 or not 1 <= max_results <= 5:
        return {"results": [], "error": "搜索词需为 1–500 字，结果数需为 1–5。"}
    bilingual = " ".join(dict.fromkeys(english for chinese, english in SEARCH_TERMS.items() if chinese in query))
    queries = [query]
    if bilingual:
        queries.append(bilingual + " " + query)
    providers = [("Bing", term, "https://www.bing.com/search?" + urlencode({"q": term, "format": "rss"})) for term in queries]
    providers.append(("DuckDuckGo", query, "https://html.duckduckgo.com/html/?" + urlencode({"q": query})))
    terms = search_terms(query)
    collected = []
    for provider, used_query, url in providers:
        try:
            _, content_type, body = fetch_public(url, max_bytes=2 * 1024 * 1024)
            if provider == "Bing":
                root = ElementTree.fromstring(body)
                candidates = [{"title": item.findtext("title", ""), "url": item.findtext("link", ""),
                               "snippet": item.findtext("description", "")}
                              for item in root.findall("./channel/item")]
            else:
                parser = PageParser()
                parser.feed(decode_page(body, content_type))
                candidates = parser.links
            results, seen = [], set()
            for candidate in candidates:
                description = (candidate["title"] + " " + candidate.get("snippet", "")).casefold()
                if terms and not any(term in description for term in terms):
                    continue
                target = urljoin(url, candidate["url"])
                parsed = urlsplit(target)
                if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
                    target = parse_qs(parsed.query).get("uddg", [""])[0]
                parsed = urlsplit(target)
                if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or target in seen:
                    continue
                seen.add(target)
                results.append({"title": candidate["title"].strip()[:200], "url": target})
                if len(results) >= max_results:
                    break
            if results:
                collected.append({"provider": provider, "query": used_query, "results": results})
                if len(results) >= min(3, max_results):
                    return collected[-1]
        except (WebError, ValueError, ElementTree.ParseError):
            continue
    if collected:
        return max(collected, key=lambda item: len(item["results"]))
    return {"results": [], "error": "公开搜索未返回相关结果，可能受到网站限流或验证限制。可直接粘贴原文链接重试。"}


def read_web_document(url: str) -> dict:
    """读取公开 HTML、纯文本或文字版 PDF；正文不代表已通过医学审核。"""
    try:
        final_url, content_type, data = fetch_public(url)
        if data.startswith(b"%PDF-") or "application/pdf" in content_type.lower():
            import pymupdf
            try:
                with pymupdf.open(stream=data, filetype="pdf") as document:
                    if document.is_encrypted or document.page_count > 80:
                        raise WebError("仅支持未加密且不超过 80 页的 PDF。")
                    pages, total = [], 0
                    for i, page in enumerate(document):
                        text = page.get_text()[:60000 - total]
                        pages.append({"page": i + 1, "text": text})
                        total += len(text)
                        if total >= 60000:
                            break
                    title = (document.metadata or {}).get("title") or urlsplit(final_url).path.rsplit("/", 1)[-1]
            except WebError:
                raise
            except Exception:
                raise WebError("PDF 无法解析。") from None
        elif any(kind in content_type.lower() for kind in ("text/html", "application/xhtml+xml", "text/plain")):
            raw = decode_page(data, content_type)
            if "text/plain" in content_type.lower():
                title, text = urlsplit(final_url).hostname, raw
            else:
                parser = PageParser()
                parser.feed(raw)
                title, text = "".join(parser.title), parser.text()
                if re.search(r"verify you are human|checking your browser|just a moment|captcha|人机验证|安全验证", title + text[:1000], re.I):
                    raise WebError("网页要求验证，未取得正文。")
                if re.search(r"登录|登錄|sign in|log in", title, re.I):
                    raise WebError("网页要求登录，未取得正文。")
            pages = [{"page": None, "text": text[:60000]}]
        else:
            raise WebError("仅支持 HTML、纯文本和 PDF。")
        if sum(len(p["text"].strip()) for p in pages) < 100:
            raise WebError("未取得足够正文，网页可能需要登录、动态加载或 OCR。")
        return {"url": final_url, "title": re.sub(r"\s+", " ", title or final_url).strip()[:200], "pages": pages,
                "fetched_at": datetime.now(timezone.utc).isoformat(), "reviewed": False,
                "document_id": hashlib.sha256(final_url.encode()).hexdigest()[:16]}
    except WebError as exc:
        return {"error": str(exc)}
