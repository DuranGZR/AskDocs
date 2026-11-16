from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from src.core.exceptions import ServiceUnavailableError
from src.services.base import BaseRateLimiter, RateLimitDecision

logger = logging.getLogger(__name__)

_SAFE_SCOPE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_SAFE_SUBJECT_RE = re.compile(r"^[a-zA-Z0-9_:-]{1,64}$")

# Atomik kayan pencere (Sliding Window Log) Lua betiği:
# - Pencere dışı eski kayıtları temizler.
# - Kota doluysa yeni kayıt eklemez (429 döngüsünde cezayı uzatmaz) ve kalan süreyi hesaplar.
# - Kota uygunsa benzersiz üyeyi ekler ve pencere süresince TTL atar.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now_ms - window_ms

redis.call("ZREMRANGEBYSCORE", key, "-inf", cutoff)
local count = redis.call("ZCARD", key)

if count >= limit then
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    local retry_after_ms = window_ms
    if oldest[2] then
        retry_after_ms = math.max(1, math.floor(tonumber(oldest[2]) + window_ms - now_ms))
    end
    redis.call("PEXPIRE", key, window_ms)
    return {0, 0, retry_after_ms}
end

redis.call("ZADD", key, now_ms, member)
redis.call("PEXPIRE", key, window_ms)
local remaining = math.max(0, limit - (count + 1))
local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
local reset_ms = window_ms
if oldest[2] then
    reset_ms = math.max(1, math.floor(tonumber(oldest[2]) + window_ms - now_ms))
end
return {1, remaining, reset_ms}
"""


class AsyncRedisLike(Protocol):
    """RedisRateLimiter ile uyumlu asenkron Redis istemci protokolü."""

    def register_script(self, script: str) -> Any: ...

    async def aclose(self) -> None: ...


def build_rate_limit_subject(*, auth_digest: str, client_ip: str) -> str:
    """API anahtarı özeti ve istemci IP adresinden gizlilik dostu tekil anahtar üretir."""
    normalized_ip = (client_ip or "unknown").strip() or "unknown"
    raw = f"{auth_digest}|{normalized_ip}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _sanitize_key_part(value: str, *, pattern: re.Pattern[str], label: str) -> str:
    cleaned = value.strip()
    if pattern.fullmatch(cleaned):
        return cleaned
    logger.debug("Hashing non-standard rate limit %s component", label)
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:24]


class RedisRateLimiter(BaseRateLimiter):
    """Redis üzerinde atomik Lua kayan pencere algoritmasıyla çalışan hız sınırlayıcı."""

    def __init__(
        self,
        *,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "corerag:rl",
        timeout_seconds: float = 1.0,
        fail_open: bool = True,
        client: AsyncRedisLike | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._owns_client = client is None
        self._client: Any = (
            client
            if client is not None
            else aioredis.Redis.from_url(
                redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=timeout_seconds,
                socket_timeout=timeout_seconds,
            )
        )
        self._key_prefix = key_prefix.strip().rstrip(":") or "corerag:rl"
        self._fail_open = fail_open
        self._clock = clock
        self._script = self._client.register_script(_SLIDING_WINDOW_LUA)

    async def check_and_consume(
        self,
        *,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Redis Lua betiği üzerinden kotayı kontrol eder ve izin verilirse düşer."""
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive integers")

        safe_scope = _sanitize_key_part(scope, pattern=_SAFE_SCOPE_RE, label="scope")
        safe_subject = _sanitize_key_part(subject, pattern=_SAFE_SUBJECT_RE, label="subject")
        redis_key = f"{self._key_prefix}:{safe_scope}:{safe_subject}"

        now_ms = int(self._clock() * 1000)
        window_ms = window_seconds * 1000
        member = f"{now_ms}:{uuid.uuid4().hex[:12]}"

        try:
            raw_result = await self._script(
                keys=[redis_key],
                args=[now_ms, window_ms, limit, member],
            )
            allowed_flag = bool(int(raw_result[0]))
            remaining = max(0, int(raw_result[1]))
            ttl_ms = max(1, int(raw_result[2]))
            ttl_seconds = max(1, math.ceil(ttl_ms / 1000.0))
        except (RedisError, OSError, TimeoutError) as exc:
            if self._fail_open:
                logger.warning(
                    "Rate limiter Redis unavailable (fail-open active, scope=%s): %s",
                    safe_scope,
                    type(exc).__name__,
                )
                return RateLimitDecision(
                    allowed=True,
                    limit=limit,
                    remaining=limit,
                    reset_after_seconds=window_seconds,
                    retry_after_seconds=0,
                )
            logger.error(
                "Rate limiter Redis unavailable (fail-closed, scope=%s): %s",
                safe_scope,
                type(exc).__name__,
            )
            raise ServiceUnavailableError("Rate limiter service is currently unavailable") from exc

        if allowed_flag:
            return RateLimitDecision(
                allowed=True,
                limit=limit,
                remaining=remaining,
                reset_after_seconds=ttl_seconds,
                retry_after_seconds=0,
            )

        return RateLimitDecision(
            allowed=False,
            limit=limit,
            remaining=0,
            reset_after_seconds=ttl_seconds,
            retry_after_seconds=ttl_seconds,
        )

    async def close(self) -> None:
        """Kendi oluşturduğu asenkron Redis bağlantı havuzunu kapatır."""
        if self._owns_client:
            await self._client.aclose()


class InMemoryRateLimiter(BaseRateLimiter):
    """Testler ve harici Redis olmayan ortamlar için atomik bellek içi kayan pencere."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = {}

    async def check_and_consume(
        self,
        *,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
    ) -> RateLimitDecision:
        """Bellek içi kayan pencere ile kotayı atomik olarak doğrular."""
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive integers")

        now = self._clock()
        cutoff = now - float(window_seconds)
        bucket_key = (scope, subject)

        async with self._lock:
            hits = self._buckets.get(bucket_key)
            if hits is None:
                hits = deque()
                self._buckets[bucket_key] = hits

            while hits and hits[0] <= cutoff:
                hits.popleft()

            if len(hits) >= limit:
                retry_seconds = max(1, math.ceil(hits[0] + window_seconds - now))
                return RateLimitDecision(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    reset_after_seconds=retry_seconds,
                    retry_after_seconds=retry_seconds,
                )

            hits.append(now)
            remaining = max(0, limit - len(hits))
            reset_seconds = max(1, math.ceil(hits[0] + window_seconds - now))
            return RateLimitDecision(
                allowed=True,
                limit=limit,
                remaining=remaining,
                reset_after_seconds=reset_seconds,
                retry_after_seconds=0,
            )

    async def close(self) -> None:
        """Bellek içi kayıtları temizler."""
        async with self._lock:
            self._buckets.clear()
