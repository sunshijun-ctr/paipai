from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseKVStore(ABC):
    """Key-value store interface (Redis-compatible)."""

    @abstractmethod
    async def get(self, key: str) -> Optional[str]:
        pass

    @abstractmethod
    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        pass

    @abstractmethod
    async def delete(self, key: str) -> None:
        pass

    @abstractmethod
    async def hset(self, name: str, key: str, value: str) -> None:
        pass

    @abstractmethod
    async def hget(self, name: str, key: str) -> Optional[str]:
        pass

    @abstractmethod
    async def hgetall(self, name: str) -> dict[str, str]:
        pass


class BaseVectorStore(ABC):
    """Vector store interface (Chroma-compatible)."""

    @abstractmethod
    async def add(
        self,
        collection: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        pass

    @abstractmethod
    async def query(
        self,
        collection: str,
        query_texts: list[str],
        n_results: int = 5,
        where: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def delete_collection(self, collection: str) -> None:
        pass

    @abstractmethod
    async def list_collections(self) -> list[str]:
        pass
