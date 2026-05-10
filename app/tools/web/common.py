import asyncio
import datetime as dt
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config.settings import settings

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>'\"）)】\]]+")
_GENERIC_IMAGE_WORDS = {"banner", "logo", "icon", "avatar", "spacer", "ads", "advertisement"}
_CONTENT_IMAGE_MARKERS = (
    "figure", "fig.", "architecture", "pipeline", "workflow", "diagram", "chart",
    "result", "experiment", "table", "图", "架构", "流程", "实验", "结果", "示意",
)


def extract_urls(text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,;，。；")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def is_mostly_chinese(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return False
    chinese = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    return chinese / max(len(chars), 1) > 0.3


def should_bypass_web_cache(query: str) -> bool:
    q = (query or "").lower()
    markers = ("最新", "今天", "实时", "刚刚", "现在", "latest", "today", "real-time", "realtime", "current")
    return any(marker in q for marker in markers)


def web_cache_key(url: str) -> str:
    today = dt.date.today().isoformat()
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", url).strip("_")[:160]
    return f"ra:web:{today}:{safe}"


def classify_content_type(url: str, content_type: str = "") -> str:
    lower_url = url.lower()
    ct = content_type.lower()
    host = urlparse(url).netloc.lower()
    if lower_url.endswith(".pdf") or "application/pdf" in ct:
        return "pdf"
    if "github.com" in host:
        return "github"
    if any(token in host for token in ("docs.", "readthedocs", "gitbook", "developer.", "learn.")):
        return "docs"
    if "text/html" in ct or not ct:
        return "article"
    return "unknown"


def relevance_score(query: str, *texts: str) -> float:
    terms = {t for t in re.findall(r"[a-zA-Z0-9_\-\u4e00-\u9fff]{2,}", (query or "").lower())}
    if not terms:
        return 0.0
    haystack = " ".join(texts).lower()
    hits = sum(1 for term in terms if term in haystack)
    return hits / max(len(terms), 1)


async def fetch_url_metadata(url: str, user_query: str = "") -> dict[str, Any]:
    headers = {"User-Agent": "ResearchAssistant/1.0 (+web_read)"}
    title = ""
    description = ""
    accessible = False
    content_type_header = ""
    status_code = 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12, headers=headers) as client:
            try:
                head = await client.head(url)
                status_code = head.status_code
                content_type_header = head.headers.get("content-type", "")
                accessible = status_code < 400
            except Exception:
                pass
            if not accessible or "text/html" in content_type_header.lower() or not content_type_header:
                resp = await client.get(url)
                status_code = resp.status_code
                content_type_header = resp.headers.get("content-type", content_type_header)
                accessible = status_code < 400
                if "text/html" in content_type_header.lower():
                    soup = BeautifulSoup(resp.text[:250_000], "html.parser")
                    title = _clean_text(
                        (soup.find("meta", property="og:title") or {}).get("content")
                        or (soup.find("title").get_text(" ", strip=True) if soup.find("title") else "")
                    )
                    description = _clean_text(
                        (soup.find("meta", attrs={"name": "description"}) or {}).get("content")
                        or (soup.find("meta", property="og:description") or {}).get("content")
                        or ""
                    )
    except Exception as exc:
        logger.debug("URL metadata fetch failed for %s: %s", url, exc)

    content_type = classify_content_type(url, content_type_header)
    score = relevance_score(user_query, title, description, url)
    recommended = accessible and content_type != "unknown" and (not user_query or score > 0 or len(title + description) > 0)
    return {
        "url": url,
        "title": title or url,
        "description": description,
        "accessible": accessible,
        "status_code": status_code,
        "content_type": content_type,
        "recommended": recommended,
        "score": score,
    }


async def scrape_url(url: str, extract_images: bool = True, extract_code: bool = True) -> dict[str, Any]:
    if settings.firecrawl_api_key:
        try:
            return await _scrape_with_firecrawl(url, extract_images=extract_images, extract_code=extract_code)
        except Exception as exc:
            logger.info("Firecrawl scrape failed for %s; falling back: %s", url, exc)

    try:
        return await _scrape_with_crawl4ai(url)
    except Exception as exc:
        logger.debug("Crawl4AI scrape unavailable/failed for %s: %s", url, exc)

    return await _scrape_with_http(url, extract_images=extract_images, extract_code=extract_code)


async def _scrape_with_firecrawl(url: str, extract_images: bool, extract_code: bool) -> dict[str, Any]:
    endpoint = settings.firecrawl_base_url.rstrip("/") + "/v1/scrape"
    headers = {
        "Authorization": f"Bearer {settings.firecrawl_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": url,
        "formats": ["markdown", "html"],
        "onlyMainContent": True,
        "waitFor": 1000,
    }
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        resp = await client.post(endpoint, headers=headers, json=payload)
        resp.raise_for_status()
        raw = resp.json()
    data = raw.get("data", raw)
    markdown = data.get("markdown") or ""
    html = data.get("html") or ""
    metadata = data.get("metadata") or {}
    title = metadata.get("title") or metadata.get("ogTitle") or url
    parsed = _parse_html_content(url, html, extract_images=extract_images, extract_code=extract_code) if html else {}
    text = markdown.strip() or parsed.get("text", "")
    code_blocks = parsed.get("code_blocks", []) if extract_code else []
    images = parsed.get("images", []) if extract_images else []
    return _scrape_payload(url, title, text, code_blocks, images, source="firecrawl")


async def _scrape_with_crawl4ai(url: str) -> dict[str, Any]:
    try:
        from crawl4ai import AsyncWebCrawler  # type: ignore
    except Exception as exc:
        raise RuntimeError("crawl4ai is not installed") from exc

    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
    text = getattr(result, "markdown", "") or getattr(result, "cleaned_html", "") or ""
    return _scrape_payload(url, url, text, [], [], source="crawl4ai")


async def _scrape_with_http(url: str, extract_images: bool, extract_code: bool) -> dict[str, Any]:
    headers = {"User-Agent": "ResearchAssistant/1.0 (+web_read)"}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    content_type = resp.headers.get("content-type", "")
    if "application/pdf" in content_type.lower() or url.lower().endswith(".pdf"):
        return _scrape_payload(url, url, f"PDF URL: {url}", [], [], source="pdf")
    parsed = _parse_html_content(url, resp.text, extract_images=extract_images, extract_code=extract_code)
    return _scrape_payload(url, parsed.get("title") or url, parsed.get("text", ""), parsed.get("code_blocks", []), parsed.get("images", []), source="http")


def _parse_html_content(url: str, html: str, extract_images: bool, extract_code: bool) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    title = _clean_text(
        (soup.find("meta", property="og:title") or {}).get("content")
        or (soup.find("title").get_text(" ", strip=True) if soup.find("title") else "")
    )
    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "form", "svg"]):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    code_blocks = _extract_code_blocks(root) if extract_code else []
    images = _extract_images(root, url) if extract_images else []
    text = _html_to_markdownish(root)
    return {"title": title, "text": text, "code_blocks": code_blocks, "images": images}


def _extract_code_blocks(root) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    for node in root.find_all(["pre", "code"]):
        if node.find_parent("pre") and node.name == "code":
            continue
        content = node.get_text("\n", strip=True)
        if len(content) < 20:
            continue
        language = ""
        classes = " ".join(node.get("class", []) or [])
        match = re.search(r"language-([a-zA-Z0-9_+-]+)", classes)
        if match:
            language = match.group(1)
        blocks.append({"language": language, "content": content})
    return blocks[:20]


def _extract_images(root, base_url: str) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for img in root.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        alt = _clean_text(img.get("alt") or "")
        caption = ""
        figure = img.find_parent("figure")
        if figure:
            cap = figure.find("figcaption")
            if cap:
                caption = _clean_text(cap.get_text(" ", strip=True))
        score = _image_score(alt, caption)
        images.append({
            "url": urljoin(base_url, src),
            "alt": alt,
            "caption": caption,
            "worth_reading": score > 0,
            "score": score,
        })
    images.sort(key=lambda item: item.get("score", 0), reverse=True)
    return images[:12]


def _image_score(alt: str, caption: str) -> int:
    text = f"{alt} {caption}".strip().lower()
    if not text or text in _GENERIC_IMAGE_WORDS:
        return 0
    score = 1
    for marker in _CONTENT_IMAGE_MARKERS:
        if marker.lower() in text:
            score += 2
    if caption:
        score += 2
    return score


def _html_to_markdownish(root) -> str:
    lines: list[str] = []
    for node in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "pre", "table"]):
        text = _clean_text(node.get_text("\n" if node.name == "pre" else " ", strip=True))
        if not text:
            continue
        if node.name == "h1":
            lines.append(f"# {text}")
        elif node.name == "h2":
            lines.append(f"## {text}")
        elif node.name == "h3":
            lines.append(f"### {text}")
        elif node.name == "li":
            lines.append(f"- {text}")
        elif node.name == "blockquote":
            lines.append(f"> {text}")
        elif node.name == "pre":
            lines.append(f"```\n{text}\n```")
        else:
            lines.append(text)
    return "\n\n".join(_dedupe_lines(lines))


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _clean_text(text: Any) -> str:
    return re.sub(r"\s+", " ", unescape(str(text or ""))).strip()


def _scrape_payload(url: str, title: str, text: str, code_blocks: list[dict[str, str]], images: list[dict[str, Any]], source: str) -> dict[str, Any]:
    text = (text or "").strip()
    return {
        "url": url,
        "title": title or url,
        "text": text,
        "code_blocks": code_blocks,
        "images": images,
        "char_count": len(text),
        "truncated": False,
        "source": source,
    }


async def gather_limited(tasks, limit: int = 5):
    semaphore = asyncio.Semaphore(limit)

    async def run_task(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(run_task(task) for task in tasks), return_exceptions=True)
