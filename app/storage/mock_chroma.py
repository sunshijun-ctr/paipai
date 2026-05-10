from typing import Any, Optional
from .base import BaseVectorStore


class MockChroma(BaseVectorStore):
    """In-memory Chroma mock. No real embeddings — stores raw text for dev/testing."""

    def __init__(self) -> None:
        self._collections: dict[str, list[dict[str, Any]]] = {}

    async def add(
        self,
        collection: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        if collection not in self._collections:
            self._collections[collection] = []
        for doc, meta, doc_id in zip(documents, metadatas, ids):
            self._collections[collection].append(
                {"id": doc_id, "document": doc, "metadata": meta}
            )

    async def query(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int = 5,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        items = self._collections.get(collection, [])
        if where:
            items = [
                item for item in items
                if all(item["metadata"].get(k) == v for k, v in where.items())
            ]
        # Mock: return first n_results items (no real similarity search)
        return items[:n_results]

    async def delete_collection(self, collection: str) -> None:
        self._collections.pop(collection, None)

    async def list_collections(self) -> list[str]:
        return list(self._collections.keys())
