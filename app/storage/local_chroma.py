"""Persistent local Chroma store — no external server required.

Uses chromadb.PersistentClient which stores data on disk and applies real
ONNX all-MiniLM-L6-v2 embeddings, giving genuine semantic similarity search.

This is used exclusively for long-term RAG (lt_docs + lt_memory).
Temporary RAG continues to use MockChroma (in-memory, per session).
"""
import asyncio
import logging
from typing import Any, Optional

from app.storage.base import BaseVectorStore

logger = logging.getLogger(__name__)


class LocalChromaStore(BaseVectorStore):
    """chromadb.PersistentClient backed vector store with real embeddings."""

    def __init__(self, path: str) -> None:
        import chromadb
        self._client = chromadb.PersistentClient(path=path)

    def _col(self, name: str):
        """Get or create a named collection (uses default ONNX embedding fn)."""
        return self._client.get_or_create_collection(name=name)

    def get_raw_collection(self, name: str):
        """Return the raw chromadb Collection for advanced operations (delete by where, get all)."""
        return self._col(name)

    async def add(
        self,
        collection: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        col = self._col(collection)
        await asyncio.to_thread(
            col.upsert, documents=documents, metadatas=metadatas, ids=ids
        )

    async def query(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int = 5,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        col = self._col(collection)
        count = await asyncio.to_thread(col.count)
        if count == 0:
            return []
        n = min(n_results, count)
        kwargs: dict[str, Any] = {"query_texts": query_texts, "n_results": n}
        if where:
            kwargs["where"] = where
        try:
            results = await asyncio.to_thread(col.query, **kwargs)
        except Exception as exc:
            logger.warning("LocalChromaStore.query failed: %s", exc)
            return []

        items: list[dict[str, Any]] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        ids_ = results.get("ids", [[]])[0]
        for doc, meta, id_ in zip(docs, metas, ids_):
            items.append({"id": id_, "document": doc, "metadata": meta or {}})
        return items

    async def delete_collection(self, collection: str) -> None:
        try:
            await asyncio.to_thread(self._client.delete_collection, collection)
        except Exception as exc:
            logger.debug("delete_collection '%s': %s", collection, exc)

    async def list_collections(self) -> list[str]:
        cols = await asyncio.to_thread(self._client.list_collections)
        return [c.name if hasattr(c, "name") else str(c) for c in cols]
