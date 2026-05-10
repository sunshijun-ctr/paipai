import json
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool
from app.tools.web.common import scrape_url, should_bypass_web_cache, web_cache_key


class WebScrapeTool(BaseTool):
    name = "web_scrape"
    description = "Scrape one URL into cleaned Markdown-like text, code blocks, and useful image references."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "extract_images": {"type": "boolean", "default": True},
                "extract_code": {"type": "boolean", "default": True},
                "user_query": {"type": "string"},
            },
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        url = str(kwargs.get("url") or "").strip()
        if not url:
            return ToolResult(success=False, error="url is required")
        extract_images = bool(kwargs.get("extract_images", True))
        extract_code = bool(kwargs.get("extract_code", True))
        user_query = str(kwargs.get("user_query") or "")

        cache_key = web_cache_key(url)
        if not should_bypass_web_cache(user_query):
            try:
                from app.storage.factory import get_kv_store
                cached = await get_kv_store().get(cache_key)
                if cached:
                    return ToolResult(success=True, data=json.loads(cached))
            except Exception:
                pass

        try:
            data = await scrape_url(url, extract_images=extract_images, extract_code=extract_code)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        try:
            from app.storage.factory import get_kv_store
            await get_kv_store().set(cache_key, json.dumps(data, ensure_ascii=False), ttl=24 * 3600)
        except Exception:
            pass

        return ToolResult(success=True, data=data)
