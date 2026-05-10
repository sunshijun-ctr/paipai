import json
from typing import Any

from app.config.settings import settings
from app.schemas.tool import ToolResult
from app.tools.base import BaseTool


class SearchTool(BaseTool):
    name = "search_papers"
    description = (
        "Search academic papers across multiple platforms (arXiv, Semantic Scholar, CrossRef, PubMed). "
        "Returns a deduplicated list with title, authors, abstract, DOI, PDF URL, citations, etc."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'low-light image enhancement'",
                },
                "sources": {
                    "type": "string",
                    "description": f"Comma-separated sources or 'all'. Available: arxiv, semantic, crossref, pubmed. Default: {settings.default_search_sources}",
                    "default": settings.default_search_sources,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results per source",
                    "default": settings.default_search_max_results,
                },
                "year": {
                    "type": "string",
                    "description": "Year filter for Semantic Scholar. Examples: '2023', '2022-2025', '2023-'",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.tools.search.backends import run

        # Inject API key from settings
        kwargs.setdefault("sources", settings.default_search_sources)
        kwargs.setdefault("max_results", settings.default_search_max_results)
        kwargs["semantic_scholar_api_key"] = settings.semantic_scholar_api_key or ""

        try:
            result_json = await run(kwargs)
            result = json.loads(result_json)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if "error" in result:
            return ToolResult(success=False, error=result["error"])

        return ToolResult(success=True, data=result)
