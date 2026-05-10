import logging
from typing import Any

from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class LlamaIndexTool(BaseTool):
    name = "extract_pdf"
    description = (
        "Extract text and structure from a PDF file using LlamaParse/LlamaIndex "
        "with PyMuPDF fallback. Returns full text, per-section content, per-page "
        "text, metadata, and RAG chunks for text/table/figure content."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "local_path": {
                    "type": "string",
                    "description": "Absolute or relative path to the PDF file",
                },
                "sections": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: return only these section keys (e.g. ['abstract', 'method'])",
                },
            },
            "required": ["local_path"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        import asyncio
        from app.tools.pdf.backends import extract_any

        local_path: str = kwargs.get("local_path", "")
        requested_sections: list[str] = kwargs.get("sections", [])

        if not local_path:
            return ToolResult(success=False, error="local_path is required")

        try:
            data = await asyncio.to_thread(extract_any, local_path)
        except (ValueError, ImportError) as exc:
            return ToolResult(success=False, error=str(exc))
        except Exception as exc:
            logger.exception("PDF extraction failed for %s", local_path)
            return ToolResult(success=False, error=f"Extraction error: {exc}")

        # Filter sections if caller requested specific ones
        if requested_sections:
            data["sections"] = {
                k: v for k, v in data["sections"].items()
                if any(k.startswith(req) for req in requested_sections)
            }

        logger.info(
            "Extracted %s: %d pages, sections=%s",
            local_path,
            data["page_count"],
            list(data["sections"].keys()),
        )
        return ToolResult(success=True, data=data)


# Backward-compatible alias for imports that still reference PDFExtractTool.
PDFExtractTool = LlamaIndexTool
