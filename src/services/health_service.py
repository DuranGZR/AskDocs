from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from src.services.base import BaseHealthChecker, BaseRateLimiter, BaseVectorDB
from src.services.rate_limiter import RedisRateLimiter

logger = logging.getLogger(__name__)


def is_writable_dir(path: Path) -> bool:
    """Dizinin mevcut ve yazılabilir olup olmadığını kontrol eder."""
    return path.is_dir() and os.access(path, os.W_OK)


async def check_writable_status(path: Path) -> str:
    """Dizinin yazılabilirlik durumunu health-check standardında ('ok'/'unavailable') döner."""
    try:
        storage_ok = await asyncio.to_thread(is_writable_dir, path)
    except OSError:
        return "unavailable"
    return "ok" if storage_ok else "unavailable"


class DependencyHealthChecker(BaseHealthChecker):
    """Depolama dizini, Redis ve Qdrant sağlık kontrollerini paralel yürütür."""

    def __init__(
        self,
        *,
        upload_dir: Path,
        redis_url: str,
        vector_db: BaseVectorDB,
        rate_limiter: BaseRateLimiter | None = None,
        timeout_seconds: float = 2.0,
        redis_probe: Callable[[str, float], Awaitable[bool]] | None = None,
    ) -> None:
        self._upload_dir = upload_dir
        self._redis_url = redis_url
        self._vector_db = vector_db
        self._rate_limiter = rate_limiter
        self._timeout_seconds = timeout_seconds
        self._redis_probe = redis_probe

    async def check_dependencies(self) -> dict[str, str]:
        """Depolama, Redis ve Qdrant sağlık kontrollerini eşzamanlı olarak koşar."""
        storage_status, redis_ok, qdrant_ok = await asyncio.gather(
            check_writable_status(self._upload_dir),
            self._check_redis(),
            self._check_qdrant(),
        )
        return {
            "storage": storage_status,
            "redis": "ok" if redis_ok else "unavailable",
            "qdrant": "ok" if qdrant_ok else "unavailable",
        }

    async def _check_redis(self) -> bool:
        try:
            if self._redis_probe is not None:
                return await asyncio.wait_for(
                    self._redis_probe(self._redis_url, self._timeout_seconds),
                    timeout=self._timeout_seconds + 0.1,
                )
            if isinstance(self._rate_limiter, RedisRateLimiter):
                return await self._rate_limiter.ping(timeout_seconds=self._timeout_seconds)

            client = aioredis.Redis.from_url(
                self._redis_url,
                socket_connect_timeout=self._timeout_seconds,
                socket_timeout=self._timeout_seconds,
            )
            try:
                result = await asyncio.wait_for(client.ping(), timeout=self._timeout_seconds)
                return bool(result)
            finally:
                await client.aclose()
        except (RedisError, OSError, TimeoutError, Exception) as exc:
            logger.warning("Health probe for Redis failed: %s", type(exc).__name__)
            return False

    async def _check_qdrant(self) -> bool:
        try:
            return await asyncio.wait_for(
                self._vector_db.ping(timeout_seconds=self._timeout_seconds),
                timeout=self._timeout_seconds + 0.1,
            )
        except Exception as exc:
            logger.warning("Health probe for Qdrant failed: %s", type(exc).__name__)
            return False
