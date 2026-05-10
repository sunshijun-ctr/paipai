"""AddToLibraryTool — index a document into one of the user's knowledge bases."""
import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class AddToLibraryTool(BaseTool):
    name = "add_to_library"
    description = (
        "Index a document (PDF or text file) into a named knowledge base. "
        "The document is chunked and embedded immediately so it can be retrieved "
        "via 'library_qa'. Documents persist across sessions."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_path": {"type": "string"},
                "title":      {"type": "string"},
                "lib_id":     {"type": "string",
                               "description": "Target knowledge-base ID (default: lt_docs)"},
                "chunk_size": {"type": "integer", "description": "Optional chunk size in characters"},
                "chunk_overlap": {"type": "integer", "description": "Optional chunk overlap in characters"},
                "extra_meta": {"type": "object", "description": "Optional metadata such as venue, journal, doi, authors"},
            },
            "required": ["local_path", "title"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.rag.long_term.store import get_lt_rag_store

        local_path: str = kwargs["local_path"]
        title:      str = kwargs["title"]
        lib_id:     str = kwargs.get("lib_id", "lt_docs")
        chunk_size = kwargs.get("chunk_size")
        chunk_overlap = kwargs.get("chunk_overlap")
        extra_meta = kwargs.get("extra_meta") or {}

        lt = get_lt_rag_store()
        try:
            n = await lt.add_document(
                local_path=local_path,
                title=title,
                lib_id=lib_id,
                extra_meta=extra_meta,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        except Exception as exc:
            logger.exception("AddToLibraryTool: failed for '%s'", title)
            return ToolResult(success=False, error=str(exc))

        if n == 0:
            return ToolResult(success=False,
                              error=f"No content extracted from '{title}'.")

        logger.info("AddToLibraryTool: '%s' → %d chunks in '%s'", title, n, lib_id)
        return ToolResult(success=True, data={"title": title, "chunks_indexed": n, "lib_id": lib_id})
