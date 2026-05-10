"""
CrossEncoder reranker — higher accuracy, requires sentence-transformers.

Install: pip install sentence-transformers
Models : cross-encoder/ms-marco-MiniLM-L-6-v2  (~60 MB, fast)
         cross-encoder/ms-marco-MiniLM-L-12-v2  (~120 MB, better)
         BAAI/bge-reranker-base                  (~280 MB, multilingual)
"""
import logging
from typing import Any

from app.rag.reranker.base import BaseReranker

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class CrossEncoderReranker(BaseReranker):
    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        try:
            from sentence_transformers.cross_encoder import CrossEncoder
        except ImportError as e:
            raise ImportError(
                "sentence-transformers is required for CrossEncoderReranker: "
                "pip install sentence-transformers"
            ) from e

        self._model = CrossEncoder(model_name)
        logger.info("CrossEncoderReranker loaded: %s", model_name)

    def rerank(self, query: str, chunks: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
        if not chunks:
            return []

        texts = [c.get("document", "") for c in chunks]
        pairs = [(query, t) for t in texts]
        scores = self._model.predict(pairs)

        scored = sorted(
            zip(scores, chunks),
            key=lambda x: float(x[0]),
            reverse=True,
        )
        return [{**chunk, "rerank_score": float(score)} for score, chunk in scored[:top_n]]
