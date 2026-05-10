import time
from typing import Any, Optional
from .base import BaseKVStore


class MockRedis(BaseKVStore):
    """In-memory Redis mock. TTL is checked lazily on read."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, Optional[float]]] = {}  # key -> (value, expires_at)
        self._hstore: dict[str, dict[str, str]] = {}

    def _is_expired(self, key: str) -> bool:
        if key not in self._store:
            return True
        _, expires_at = self._store[key]
        return expires_at is not None and time.time() > expires_at

    async def get(self, key: str) -> Optional[str]:
        if self._is_expired(key):
            self._store.pop(key, None)
            return None
        return self._store[key][0]

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        expires_at = time.time() + ttl if ttl else None
        self._store[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def hset(self, name: str, key: str, value: str) -> None:
        if name not in self._hstore:
            self._hstore[name] = {}
        self._hstore[name][key] = value

    async def hget(self, name: str, key: str) -> Optional[str]:
        return self._hstore.get(name, {}).get(key)

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self._hstore.get(name, {}))
