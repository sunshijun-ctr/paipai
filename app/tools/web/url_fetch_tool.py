from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool
from app.tools.web.common import fetch_url_metadata, gather_limited


class UrlFetchTool(BaseTool):
    name = "url_fetch"
    description = "Fetch lightweight URL metadata and decide whether pages are worth full web reading."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "user_query": {"type": "string"},
            },
            "required": ["urls"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        urls = [str(u).strip() for u in kwargs.get("urls", []) if str(u).strip()]
        user_query = str(kwargs.get("user_query") or "")
        if not urls:
            return ToolResult(success=False, error="urls is required")

        results = await gather_limited([fetch_url_metadata(url, user_query) for url in urls], limit=5)
        clean = []
        for result in results:
            if isinstance(result, Exception):
                continue
            clean.append(result)
        clean.sort(key=lambda item: (item.get("recommended", False), item.get("score", 0)), reverse=True)
        return ToolResult(success=True, data={"results": clean})
