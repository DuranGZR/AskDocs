from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, ParamSpec, TypeVar

from src.core.exceptions import AppError, ServiceUnavailableError

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

CircuitState = Literal["closed", "open", "half_open"]
FailurePredicate = Callable[[BaseException], bool]


def default_is_upstream_failure(exc: BaseException) -> bool:
    """Devre kesiciyi yalnızca yukarı akış altyapı hatalarında tetikler.

    İstemci kaynaklı 4xx hataları (BadRequest, Unauthorized, NotFound, 429) ve
    istemci iptalleri (CancelledError, GeneratorExit) devreyi açmamalıdır.
    """
    if isinstance(exc, asyncio.CancelledError | GeneratorExit | KeyboardInterrupt | SystemExit):
        return False
    if isinstance(exc, AppError):
        return exc.status_code >= 500
    return isinstance(exc, Exception)


@dataclass(frozen=True, slots=True)
class CircuitSnapshot:
    """Devre kesicinin anlık durum ve sayaç özeti."""

    name: str
    state: CircuitState
    failure_count: int
    half_open_successes: int
    failure_threshold: int
    recovery_timeout_seconds: float
    retry_after_seconds: int


class _CircuitStateMachine:
    """Thread-safe 3 durumlu (closed/open/half_open) çekirdek devre kesici durum makinesi."""

    def __init__(
        self,
        *,
        name: str,
        enabled: bool = True,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        half_open_success_threshold: int = 1,
        failure_predicate: FailurePredicate = default_is_upstream_failure,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise ValueError("Circuit breaker name must not be empty")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be at least 1")
        if half_open_success_threshold < 1:
            raise ValueError("half_open_success_threshold must be at least 1")

        self.name = cleaned_name
        self.enabled = enabled
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.half_open_max_calls = half_open_max_calls
        self.half_open_success_threshold = half_open_success_threshold
        self._failure_predicate = failure_predicate
        self._clock = clock

        self._lock = threading.Lock()
        self._state: CircuitState = "closed"
        self._failure_count = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        self._half_open_successes = 0

    def acquire(self) -> bool:
        """Çağrı izni ister; devre OPEN/dolu HALF_OPEN ise 503 fırlatır, probe ise True döner."""
        if not self.enabled:
            return False

        with self._lock:
            now = self._clock()
            if self._state == "open":
                elapsed = now - self._opened_at
                remaining = self.recovery_timeout_seconds - elapsed
                if remaining > 0:
                    retry_after = max(1, math.ceil(remaining))
                    raise ServiceUnavailableError(
                        f"Service '{self.name}' is temporarily unavailable "
                        f"(circuit open; retry in {retry_after}s).",
                        headers={"Retry-After": str(retry_after)},
                    )
                self._state = "half_open"
                self._half_open_in_flight = 0
                self._half_open_successes = 0
                logger.info(
                    "Circuit breaker '%s' transitioned OPEN -> HALF_OPEN after %.1fs",
                    self.name,
                    elapsed,
                )

            if self._state == "half_open":
                if self._half_open_in_flight >= self.half_open_max_calls:
                    raise ServiceUnavailableError(
                        f"Service '{self.name}' recovery probe is already in progress.",
                        headers={"Retry-After": "1"},
                    )
                self._half_open_in_flight += 1
                return True

            return False

    def record_success(self, *, is_probe: bool) -> None:
        """Başarılı çağrı sonrası hata sayacını sıfırlar veya HALF_OPEN devreyi kapatır."""
        if not self.enabled:
            return

        with self._lock:
            if self._state == "half_open":
                if is_probe and self._half_open_in_flight > 0:
                    self._half_open_in_flight -= 1
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_success_threshold:
                    self._state = "closed"
                    self._failure_count = 0
                    self._half_open_in_flight = 0
                    self._half_open_successes = 0
                    logger.info(
                        "Circuit breaker '%s' recovered (HALF_OPEN -> CLOSED)",
                        self.name,
                    )
                return

            self._failure_count = 0

    def record_failure(self, exc: BaseException, *, is_probe: bool) -> None:
        """Hata sayacını günceller; eşik aşılınca devreyi OPEN konumuna alır."""
        if not self.enabled:
            return

        counts_as_failure = self._failure_predicate(exc)
        with self._lock:
            if is_probe and self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1

            if not counts_as_failure:
                return

            if self._state == "half_open":
                self._state = "open"
                self._opened_at = self._clock()
                self._failure_count = self.failure_threshold
                self._half_open_in_flight = 0
                self._half_open_successes = 0
                logger.warning(
                    "Circuit breaker '%s' probe failed (%s); reopened HALF_OPEN -> OPEN",
                    self.name,
                    type(exc).__name__,
                )
                return

            if self._state == "closed":
                self._failure_count += 1
                if self._failure_count >= self.failure_threshold:
                    self._state = "open"
                    self._opened_at = self._clock()
                    logger.warning(
                        "Circuit breaker '%s' tripped CLOSED -> OPEN after %d failures (%s)",
                        self.name,
                        self._failure_count,
                        type(exc).__name__,
                    )

    def release_cancelled(self, *, is_probe: bool) -> None:
        """İptal veya erken akış çıkışında probe hakkını hata yazmadan serbest bırakır."""
        if not self.enabled or not is_probe:
            return

        with self._lock:
            if self._half_open_in_flight > 0:
                self._half_open_in_flight -= 1

    def snapshot(self) -> CircuitSnapshot:
        """Mevcut devre kesici durumunun anlık görüntüsünü döner."""
        with self._lock:
            retry_after = 0
            if self._state == "open":
                remaining = self.recovery_timeout_seconds - (self._clock() - self._opened_at)
                if remaining > 0:
                    retry_after = max(1, math.ceil(remaining))
            return CircuitSnapshot(
                name=self.name,
                state=self._state,
                failure_count=self._failure_count,
                half_open_successes=self._half_open_successes,
                failure_threshold=self.failure_threshold,
                recovery_timeout_seconds=self.recovery_timeout_seconds,
                retry_after_seconds=retry_after,
            )

    def reset(self) -> None:
        """Devre kesiciyi başlangıç (closed) durumuna sıfırlar."""
        with self._lock:
            self._state = "closed"
            self._failure_count = 0
            self._opened_at = 0.0
            self._half_open_in_flight = 0
            self._half_open_successes = 0


class _BaseCircuitBreaker:
    """Asenkron ve senkron devre kesiciler için ortak durum ve teşhis arayüzü."""

    def __init__(
        self,
        *,
        name: str,
        enabled: bool = True,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        half_open_max_calls: int = 1,
        half_open_success_threshold: int = 1,
        failure_predicate: FailurePredicate = default_is_upstream_failure,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._machine = _CircuitStateMachine(
            name=name,
            enabled=enabled,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            half_open_max_calls=half_open_max_calls,
            half_open_success_threshold=half_open_success_threshold,
            failure_predicate=failure_predicate,
            clock=clock,
        )

    @property
    def name(self) -> str:
        """Devre kesici bileşen adını döndürür."""
        return self._machine.name

    @property
    def enabled(self) -> bool:
        """Devre kesicinin aktif olup olmadığını döndürür."""
        return self._machine.enabled

    @property
    def state(self) -> CircuitState:
        """Anlık devre durumunu ('closed' | 'open' | 'half_open') döndürür."""
        return self._machine.snapshot().state

    @property
    def failure_count(self) -> int:
        """Ardışık başarısız çağrı sayacını döndürür."""
        return self._machine.snapshot().failure_count

    def snapshot(self) -> CircuitSnapshot:
        """Devre kesici durum özeti nesnesini döndürür."""
        return self._machine.snapshot()

    def reset(self) -> None:
        """Devre kesici sayaç ve durumunu sıfırlar."""
        self._machine.reset()


class CircuitBreaker(_BaseCircuitBreaker):
    """FastAPI ve asenkron I/O çağrıları için 3 durumlu devre kesici."""

    async def call(
        self,
        func: Callable[P, Awaitable[R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Tekil bir asenkron fonksiyon çağrısını devre kesici korumasıyla yürütür."""
        is_probe = self._machine.acquire()
        try:
            result = await func(*args, **kwargs)
        except asyncio.CancelledError:
            self._machine.release_cancelled(is_probe=is_probe)
            raise
        except BaseException as exc:
            self._machine.record_failure(exc, is_probe=is_probe)
            raise
        else:
            self._machine.record_success(is_probe=is_probe)
            return result

    async def stream(
        self,
        iterator_factory: Callable[[], AsyncIterator[T]],
    ) -> AsyncIterator[T]:
        """SSE/token akışını tüm ömrü boyunca korur; istemci iptalini hata saymaz."""
        is_probe = self._machine.acquire()
        completed = False
        cancelled = False
        try:
            async for item in iterator_factory():
                yield item
            completed = True
        except (asyncio.CancelledError, GeneratorExit):
            cancelled = True
            raise
        except BaseException as exc:
            self._machine.record_failure(exc, is_probe=is_probe)
            raise
        finally:
            if cancelled:
                self._machine.release_cancelled(is_probe=is_probe)
            elif completed:
                self._machine.record_success(is_probe=is_probe)


class SyncCircuitBreaker(_BaseCircuitBreaker):
    """Celery worker senkron istemcileri için 3 durumlu devre kesici."""

    def call(
        self,
        func: Callable[P, R],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> R:
        """Tekil bir senkron fonksiyon çağrısını devre kesici korumasıyla yürütür."""
        is_probe = self._machine.acquire()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            self._machine.record_failure(exc, is_probe=is_probe)
            raise
        else:
            self._machine.record_success(is_probe=is_probe)
            return result
