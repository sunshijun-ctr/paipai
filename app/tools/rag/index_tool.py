"""IndexPDFTool — extract a PDF and index it into the session's temp Chroma collection."""
import asyncio
import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class IndexPDFTool(BaseTool):
    name = "index_pdf"
    description = (
        "Extract text from a PDF and index it into the session's temporary "
        "Chroma collection for RAG-based question answering."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "Path to the PDF file"},
                "title": {"type": "string", "description": "Paper title (used as metadata + chunk ID prefix)"},
                "collection_name": {"type": "string", "description": "Chroma collection name (default: rag_papers)"},
                "chunk_size": {"type": "integer", "description": "Optional chunk size in characters"},
                "chunk_overlap": {"type": "integer", "description": "Optional chunk overlap in characters"},
            },
            "required": ["local_path", "title", "collection_name"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.tools.pdf.backends import extract_any
        from app.storage.factory import get_vector_store
        from app.rag.temporary.indexer import index_pdf

        local_path: str = kwargs["local_path"]
        title: str = kwargs["title"]
        collection_name: str = kwargs["collection_name"]
        chunk_size = kwargs.get("chunk_size")
        chunk_overlap = kwargs.get("chunk_overlap")

        try:
            data = await asyncio.to_thread(extract_any, local_path)
        except Exception as exc:
            return ToolResult(success=False, error=f"PDF extraction failed: {exc}")

        sections: dict[str, str] = data.get("sections", {})
        if not sections and data.get("full_text"):
            sections = {"body": data.get("full_text", "")}
        if not sections:
            return ToolResult(success=False, error="No sections extracted from PDF")

        try:
            store = get_vector_store()
            n_chunks = await index_pdf(
                sections=sections,
                title=title,
                collection=collection_name,
                store=store,
                rag_chunks=data.get("rag_chunks", []),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
        except Exception as exc:
            logger.exception("Indexing failed for '%s'", title)
            return ToolResult(success=False, error=f"Indexing failed: {exc}")

        logger.info("Indexed '%s': %d chunks into '%s'", title, n_chunks, collection_name)
        return ToolResult(
            success=True,
            data={"chunks_indexed": n_chunks, "collection": collection_name, "title": title},
        )
