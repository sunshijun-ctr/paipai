"""Returns the configured reranker instance (singleton)."""
import logging

from app.rag.reranker.base import BaseReranker

logger = logging.getLogger(__name__)

_instance: BaseReranker | None = None


def get_reranker() -> BaseReranker | None:
    """
    Return the configured reranker, or None if reranking is disabled.
    RERANKER_TYPE values: "flashrank" | "cross_encoder" | "none"
    """
    global _instance
    if _instance is not None:
        return _instance

    from app.config.settings import settings

    rtype = settings.reranker_type.lower()

    if rtype == "none":
        return None

    if rtype == "flashrank":
        from app.rag.reranker.flashrank_reranker import FlashRankReranker
        _instance = FlashRankReranker(model_name=settings.reranker_model)

    elif rtype == "cross_encoder":
        from app.rag.reranker.cross_encoder_reranker import CrossEncoderReranker
        _instance = CrossEncoderReranker(model_name=settings.reranker_model)

    else:
        logger.warning("Unknown RERANKER_TYPE '%s', disabling reranker.", rtype)
        return None

    return _instance
