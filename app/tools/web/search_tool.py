from typing import Any, Optional

import httpx

from app.config.settings import settings
from app.schemas.tool import ToolResult
from app.tools.base import BaseTool
from app.tools.web.common import is_mostly_chinese


def _fmt_exc(exc: BaseException) -> str:
    """Some httpx exceptions (e.g. ConnectError(''), ReadTimeout()) stringify to
    an empty string, which gives "serper: | tavily: " — useless. Always include
    the exception class name so the cause is visible."""
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _http_client(timeout: float = 20.0) -> httpx.AsyncClient:
    """Build an httpx client that honours WEB_SEARCH_PROXY for these outbound
    calls only (Tavily / Serper are both blocked / slow from mainland China —
    users in CN typically need an HTTP/HTTPS proxy for these two endpoints)."""
    proxy = (settings.web_search_proxy or "").strip() or None
    if proxy:
        return httpx.AsyncClient(timeout=timeout, proxy=proxy)
    return httpx.AsyncClient(timeout=timeout)


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the web with Tavily for English-heavy queries and Serper for Chinese-heavy queries."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "user_query_original": {"type": "string", "description": "Original user question"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = str(kwargs.get("query") or "").strip()
        original = str(kwargs.get("user_query_original") or query)
        max_results = int(kwargs.get("max_results") or 10)
        if not query:
            return ToolResult(success=False, error="query is required")

        try:
            results = await search_web_with_fallback(query, original, max_results)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        return ToolResult(success=True, data={"results": results})


async def search_web_with_fallback(query: str, original: str, max_results: int) -> list[dict[str, str]]:
    use_serper = is_mostly_chinese(original)
    backends = []
    if use_serper:
        backends = ["serper", "tavily"]
    else:
        backends = ["tavily", "serper"]

    errors: list[str] = []
    for backend in backends:
        try:
            if backend == "serper" and settings.serper_api_key:
                results = await _search_serper(query, max_results)
            elif backend == "tavily" and settings.tavily_api_key:
                results = await _search_tavily(query, max_results)
            else:
                continue
            if results:
                return results
            errors.append(f"{backend}: no results")
        except Exception as exc:
            errors.append(f"{backend}: {_fmt_exc(exc)}")

    if not settings.tavily_api_key and not settings.serper_api_key:
        raise RuntimeError("Missing TAVILY_API_KEY or SERPER_API_KEY")
    # If we couldn't reach any backend AND there's no proxy configured, point
    # the user at the most likely cause — mainland network can't reach
    # api.tavily.com / google.serper.dev without one.
    hint = ""
    if not settings.web_search_proxy and any("ConnectError" in e or "Timeout" in e for e in errors):
        hint = " (set WEB_SEARCH_PROXY in .env if you're behind a network that can't reach api.tavily.com / google.serper.dev directly)"
    raise RuntimeError("Web search failed on all configured backends: " + " | ".join(errors) + hint)


async def _search_tavily(query: str, max_results: int) -> list[dict[str, str]]:
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "include_raw_content": False,
    }
    async with _http_client(timeout=20) as client:
        resp = await client.post("https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", ""),
            "source": "tavily",
        }
        for item in data.get("results", [])
        if item.get("url")
    ]


async def _search_serper(query: str, max_results: int) -> list[dict[str, str]]:
    headers = {"X-API-KEY": settings.serper_api_key or "", "Content-Type": "application/json"}
    payload = {"q": query, "num": max_results}
    async with _http_client(timeout=20) as client:
        resp = await client.post("https://google.serper.dev/search", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    organic = data.get("organic", [])
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "snippet": item.get("snippet", ""),
            "source": "serper",
        }
        for item in organic
        if item.get("link")
    ]
