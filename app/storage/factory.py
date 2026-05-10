"""Returns the configured vector/KV store (real or in-memory mock)."""
import logging

from app.storage.base import BaseKVStore, BaseVectorStore

logger = logging.getLogger(__name__)

_vector_instance: BaseVectorStore | None = None
_kv_instance: BaseKVStore | None = None


def get_vector_store() -> BaseVectorStore:
    global _vector_instance
    if _vector_instance is not None:
        return _vector_instance

    from app.config.settings import settings

    if settings.use_mock_storage:
        from app.storage.mock_chroma import MockChroma
        _vector_instance = MockChroma()
    else:
        from app.storage.chroma_store import ChromaVectorStore
        _vector_instance = ChromaVectorStore()

    return _vector_instance


def get_kv_store() -> BaseKVStore:
    """Return a real Redis client, falling back to in-process MockRedis."""
    global _kv_instance
    if _kv_instance is not None:
        return _kv_instance

    from app.config.settings import settings

    if not settings.use_mock_storage and settings.redis_url:
        try:
            from app.storage.redis_store import RedisStore
            _kv_instance = RedisStore(settings.redis_url)
            logger.info("KV store: Redis at %s", settings.redis_url)
            return _kv_instance
        except Exception as exc:
            logger.warning("Redis unavailable (%s), falling back to MockRedis", exc)

    from app.storage.mock_redis import MockRedis
    _kv_instance = MockRedis()
    logger.info("KV store: MockRedis (in-process)")
    return _kv_instance
