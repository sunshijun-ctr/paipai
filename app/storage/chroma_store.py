"""Real Chroma vector store implementing BaseVectorStore."""
import asyncio
import logging
import os
from typing import Any, Optional

from app.storage.base import BaseVectorStore

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    import chromadb
    from app.config.settings import settings

    chroma_path = os.path.join(settings.data_dir, "chroma")
    os.makedirs(chroma_path, exist_ok=True)
    _client = chromadb.PersistentClient(path=chroma_path)
    logger.info("ChromaDB PersistentClient initialized at %s", chroma_path)
    return _client


class ChromaVectorStore(BaseVectorStore):
    """Persistent Chroma-backed vector store."""

    def _get_collection(self, name: str):
        client = _get_client()
        return client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    async def add(
        self,
        collection: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        def _upsert():
            col = self._get_collection(collection)
            col.upsert(documents=documents, metadatas=metadatas, ids=ids)

        await asyncio.to_thread(_upsert)

    async def query(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int = 5,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        def _query():
            col = self._get_collection(collection)
            count = col.count()
            if count == 0:
                return []
            n = min(n_results, count)
            kwargs: dict[str, Any] = {
                "query_texts": query_texts,
                "n_results": n,
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            return col.query(**kwargs)

        raw = await asyncio.to_thread(_query)
        if not raw:
            return []

        docs = raw.get("documents", [[]])[0]
        metas = raw.get("metadatas", [[]])[0]
        dists = raw.get("distances", [[]])[0]
        ids_out = raw.get("ids", [[]])[0]

        return [
            {"id": id_, "document": doc, "metadata": meta, "distance": dist}
            for id_, doc, meta, dist in zip(ids_out, docs, metas, dists)
        ]

    async def delete_collection(self, collection: str) -> None:
        def _delete():
            client = _get_client()
            try:
                client.delete_collection(collection)
            except Exception:
                pass

        await asyncio.to_thread(_delete)

    async def list_collections(self) -> list[str]:
        def _list():
            client = _get_client()
            return [c.name for c in client.list_collections()]

        return await asyncio.to_thread(_list)
