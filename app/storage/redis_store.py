from typing import Optional
from .base import BaseKVStore


class RedisStore(BaseKVStore):
    """Async Redis backed by redis-py."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis
        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> Optional[str]:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        await self._client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def hset(self, name: str, key: str, value: str) -> None:
        await self._client.hset(name, key, value)

    async def hget(self, name: str, key: str) -> Optional[str]:
        return await self._client.hget(name, key)

    async def hgetall(self, name: str) -> dict[str, str]:
        return await self._client.hgetall(name)
