import time
from abc import ABC, abstractmethod


class Deduplicator(ABC):
    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True when key has already been processed."""

    @abstractmethod
    def mark(self, key: str) -> None:
        """Mark key as processed."""


class InMemoryDeduplicator(Deduplicator):
    def __init__(self, ttl_seconds: int = 600) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, float] = {}

    def exists(self, key: str) -> bool:
        self._prune()
        return key in self._cache

    def mark(self, key: str) -> None:
        self._prune()
        self._cache[key] = time.time() + self.ttl_seconds

    def _prune(self) -> None:
        now = time.time()
        expired = [key for key, expires_at in self._cache.items() if expires_at <= now]
        for key in expired:
            self._cache.pop(key, None)
