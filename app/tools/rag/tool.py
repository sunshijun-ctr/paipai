"""RAGRetrieveTool — query the session's temporary Chroma collection."""
import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class RAGRetrieveTool(BaseTool):
    name = "rag_retrieve"
    description = "Retrieve relevant chunks from the session's temporary Chroma collection."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "collection": {"type": "string", "description": "Chroma collection name"},
                "n_results": {"type": "integer", "default": 6},
                "title_filter": {"type": "string", "default": "", "description": "Restrict to chunks from this paper"},
            },
            "required": ["query", "collection"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.storage.factory import get_vector_store
        from app.rag.temporary.retriever import retrieve

        query: str = kwargs["query"]
        collection_name: str = kwargs["collection"]
        n_results: int = int(kwargs.get("n_results", 6))
        title_filter: str = kwargs.get("title_filter", "")

        try:
            store = get_vector_store()
            chunks = await retrieve(
                query=query,
                collection=collection_name,
                store=store,
                top_n=n_results,
                title_filter=title_filter,
            )
        except Exception as exc:
            logger.warning("RAG retrieve failed: %s", exc)
            return ToolResult(success=False, error=str(exc))

        return ToolResult(success=True, data={"chunks": chunks, "collection": collection_name})
