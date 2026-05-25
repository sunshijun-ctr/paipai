"""LibrarySearchTool — hybrid RAG search over the user's permanent
literature library/libraries (the "文献管理" / 知识库).

Different from:
- ``paper_search`` — searches EXTERNAL APIs (Semantic Scholar / arXiv)
- ``rag_retrieve`` — searches the session's TEMPORARY collection of papers
                     just downloaded this turn
- ``note_search`` — searches the user's free-form text notes (NOT papers)

This tool searches the user's persistent indexed paper libraries — what
the rest of the app calls "knowledge base" or "library". Useful when the
user asks something like "我的文献里有没有关于 X 的内容" or "search
my library for Y".
"""
import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class LibrarySearchTool(BaseTool):
    name = "library_search"
    description = (
        "Search the user's permanent literature library (uploaded/indexed "
        "papers) for relevant chunks. Use this when the user asks about "
        "content in 'my library', 'my knowledge base', '我的文献库'. "
        "NOT for external paper discovery (use paper_search) and NOT for "
        "the user's free-form notes (use note_search)."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language query.",
                },
                "lib_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional library IDs to restrict the search. "
                        "Omit to search across every library the user has."
                    ),
                    "default": [],
                },
                "top_k": {"type": "integer", "default": 8},
                "title_filter": {
                    "type": "string",
                    "default": "",
                    "description": "Restrict matches to chunks from this paper title.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.rag.long_term.store import get_lt_rag_store

        query: str = (kwargs.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, error="query is required")

        lib_ids = kwargs.get("lib_ids") or None  # None → search all
        top_k = int(kwargs.get("top_k", 8))
        title_filter = (kwargs.get("title_filter") or "").strip()

        try:
            store = get_lt_rag_store()
            chunks = await store.search_documents(
                query=query,
                lib_ids=lib_ids,
                k=top_k,
                title_filter=title_filter,
            )
        except Exception as exc:
            logger.warning("LibrarySearchTool failed: %s", exc)
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return ToolResult(
            success=True,
            data={
                "chunks": chunks,
                "total": len(chunks),
                "query": query,
            },
        )
