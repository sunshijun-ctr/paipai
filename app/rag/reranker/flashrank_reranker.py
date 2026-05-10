"""FlashRank reranker — ONNX-based, no GPU required, model ~60 MB."""
import logging
from typing import Any

from app.rag.reranker.base import BaseReranker

logger = logging.getLogger(__name__)

# ms-marco-MiniLM-L-12-v2 : best accuracy in the FlashRank default models
# ms-marco-TinyBERT-L-2-v2: fastest, smallest (~20 MB)
_DEFAULT_MODEL = "ms-marco-MiniLM-L-12-v2"


class FlashRankReranker(BaseReranker):
    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        from flashrank import Ranker
        self._ranker = Ranker(model_name=model_name)
        logger.info("FlashRankReranker loaded: %s", model_name)

    def rerank(self, query: str, chunks: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
        if not chunks:
            return []

        from flashrank import RerankRequest

        # FlashRank expects passages as list[dict] with "id" and "text"
        passages = [
            {"id": i, "text": c.get("document", ""), "meta": c.get("metadata", {})}
            for i, c in enumerate(chunks)
        ]
        request = RerankRequest(query=query, passages=passages)
        results = self._ranker.rerank(request)

        # results: list of dicts {"id", "text", "score", "meta"} sorted by score desc
        reranked: list[dict[str, Any]] = []
        for r in results[:top_n]:
            original = chunks[r["id"]]
            reranked.append({**original, "rerank_score": r["score"]})

        return reranked
