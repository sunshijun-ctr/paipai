"""Session-scoped collection naming and cleanup for temporary RAG.

Each session gets its own isolated Chroma collection + BM25 pickle so that
different sessions never share or overwrite each other's indexed papers.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def session_collection(session_id: str) -> str:
    """Return the Chroma / BM25 collection name for a session's temp RAG."""
    return f"temp_{session_id}"


async def cleanup_session(session_id: str) -> None:
    """Delete the temporary Chroma collection and BM25 pickle for a session.

    Safe to call even if the collection or file does not exist.
    Does NOT delete downloaded PDF files — those stay on disk.
    """
    from app.storage.factory import get_vector_store
    from app.config.settings import settings

    collection = session_collection(session_id)

    # ── Chroma collection ─────────────────────────────────────────────────────
    try:
        store = get_vector_store()
        await store.delete_collection(collection)
        logger.info("Deleted temp Chroma collection '%s'", collection)
    except Exception as exc:
        logger.debug("Chroma cleanup for '%s': %s", collection, exc)

    # ── BM25 pickle ───────────────────────────────────────────────────────────
    bm25_path = Path(settings.data_dir) / "bm25" / f"{collection}.pkl"
    try:
        if bm25_path.exists():
            bm25_path.unlink()
            logger.info("Deleted BM25 store for session '%s'", session_id)
    except Exception as exc:
        logger.debug("BM25 cleanup for '%s': %s", collection, exc)
