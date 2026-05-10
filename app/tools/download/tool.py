import logging
import os
from typing import Any

from app.config.settings import settings
from app.schemas.tool import ToolResult
from app.tools.base import BaseTool

logger = logging.getLogger(__name__)


class DownloadTool(BaseTool):
    name = "download_pdf"
    description = (
        "Download PDF files for a list of papers. "
        "Tries source-native → direct pdf_url → Unpaywall → Sci-Hub in order."
    )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "papers": {
                    "type": "array",
                    "description": "List of paper dicts with source, paper_id, doi, title, pdf_url",
                },
                "use_scihub": {
                    "type": "boolean",
                    "default": settings.use_scihub,
                },
            },
            "required": ["papers"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult:
        from app.tools.download.backends import run as download_run

        papers: list[dict[str, Any]] = kwargs.get("papers", [])
        use_scihub: bool = kwargs.get("use_scihub", settings.use_scihub)
        papers_dir = os.path.join(settings.data_dir, "papers")

        downloaded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for paper in papers:
            title = paper.get("title", "Unknown")
            paper_id = paper.get("paper_id", paper.get("arxiv_id", ""))
            source = paper.get("source", "arxiv")

            inputs = {
                "source": source,
                "paper_id": paper_id,
                "doi": paper.get("doi", ""),
                "title": title,
                "pdf_url": paper.get("pdf_url", ""),
                "save_path": papers_dir,
                "use_scihub": use_scihub,
                "scihub_base_url": settings.scihub_base_url,
            }

            logger.info("Downloading: [%s] %s", source, title[:60])
            result = await download_run(inputs)

            if result["success"]:
                logger.info("  ✓ %s via %s → %s", title[:50], result["strategy"], result["local_path"])
                downloaded.append({
                    "title": title,
                    "local_path": result["local_path"],
                    "paper_id": paper_id,
                    "source": source,
                    "strategy": result["strategy"],
                    "doi": paper.get("doi", ""),
                    "venue": paper.get("venue", ""),
                    "journal": paper.get("journal", ""),
                    "published_date": paper.get("published_date", ""),
                    "authors": paper.get("authors", ""),
                    "citations": paper.get("citations", 0),
                })
            else:
                logger.warning("  ✗ %s | %s", title[:50], result.get("error", ""))
                failed.append({"title": title, "paper_id": paper_id, "error": result.get("error", "")})

        return ToolResult(
            success=True,
            data={
                "downloaded_pdfs": downloaded,
                "failed": failed,
                "count": len(downloaded),
            },
        )
