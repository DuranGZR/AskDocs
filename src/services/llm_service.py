import asyncio
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from src.core.circuit_breaker import CircuitBreaker, SyncCircuitBreaker
from src.core.exceptions import AppError, ServiceUnavailableError
from src.services.base import BaseLLMClient, ChatMessage, SyncBaseLLMClient

logger = logging.getLogger(__name__)

_MAX_RATE_LIMIT_RETRIES = 3
_CHAT_MAX_RATE_LIMIT_RETRIES = 2
_CHAT_MAX_RETRY_WAIT_SECONDS = 10.0
_RETRY_IN_PATTERN = re.compile(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)
_ABNORMAL_REASON_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


@dataclass(frozen=True, slots=True)
class _SSEChunk:
    """Ayrıştırılmış tek bir SSE veri satırını temsil eder."""

    is_done: bool = False
    token: str = ""
    finish_reason: str | None = None


def _normalize_finish_reason(raw_reason: Any) -> str | None:
    """Model yanıtındaki finish_reason değerini temizler; boşsa None döner."""
    if isinstance(raw_reason, str):
        cleaned = raw_reason.strip()
        if cleaned:
            return cleaned
    return None


def _extract_upstream_error_summary(error_payload: Any) -> str:
    """Loglama için yukarı akış hata nesnesinden kısa ve güvenli bir özet çıkarır."""
    if isinstance(error_payload, dict):
        parts = [
            f"{key}={str(error_payload[key]).strip()[:80]}"
            for key in ("type", "code", "message")
            if error_payload.get(key) is not None
        ]
        if parts:
            return " ".join(parts).replace("\n", " ").replace("\r", " ")
    return str(error_payload).strip()[:160].replace("\n", " ").replace("\r", " ")


def _build_abnormal_finish_error(finish_reason: str) -> AppError:
    """Normal 'stop' dışındaki tamamlanma nedenlerini uygun AppError türüne eşler."""
    logger.warning("Upstream LLM generation stopped abnormally (finish_reason=%s)", finish_reason)
    lowered = finish_reason.lower()
    if lowered in {"length", "max_tokens"}:
        return AppError(
            "AI response reached the output length limit before completion.",
            status_code=422,
            code="stream_truncated",
        )
    if lowered in {"content_filter", "safety", "recitation", "blocklist", "prohibited_content"}:
        return AppError(
            "AI response stopped early due to content filtering.",
            status_code=422,
            code="content_filtered",
        )
    safe_reason = _ABNORMAL_REASON_SAFE_RE.sub("_", lowered).strip("_")[:32] or "unknown"
    return ServiceUnavailableError(
        f"AI inference stream terminated unexpectedly (reason={safe_reason})."
    )


def _extract_retry_delay_seconds(
    response: httpx.Response,
    attempt: int,
    *,
    fallback_base_seconds: float = 15.0,
) -> float:
    """HTTP 429 yanıtındaki Retry-After veya mesaj ipucundan bekleme süresini hesaplar."""
    retry_after = response.headers.get("retry-after")
    if retry_after is not None:
        try:
            return min(65.0, max(1.0, float(retry_after.strip()) + 1.0))
        except ValueError:
            pass

    match = _RETRY_IN_PATTERN.search(response.text)
    if match:
        return min(65.0, max(1.0, float(match.group(1)) + 1.5))

    return float(min(60.0, fallback_base_seconds * (2**attempt)))


def _build_headers(api_key: str) -> dict[str, str]:
    """OpenAI-uyumlu sunucu istekleri için standart HTTP başlıklarını hazırlar."""
    headers = {"Content-Type": "application/json"}
    cleaned_key = api_key.strip()
    if cleaned_key:
        headers["Authorization"] = f"Bearer {cleaned_key}"
    return headers


def _parse_embeddings_payload(payload: Any, expected_count: int) -> list[list[float]]:
    """OpenAI /v1/embeddings yanıtını doğrular ve girdi sırasına göre vektörleri döner."""
    if not isinstance(payload, dict):
        raise ServiceUnavailableError("Invalid embedding response format from AI server.")

    data = payload.get("data")
    if not isinstance(data, list) or len(data) != expected_count:
        raise ServiceUnavailableError("Invalid embedding response format from AI server.")

    try:
        sorted_items = [
            item
            for _, item in sorted(
                enumerate(data),
                key=lambda pair: (
                    int(pair[1]["index"]) if pair[1].get("index") is not None else pair[0]
                ),
            )
        ]
        vectors: list[list[float]] = []
        for item in sorted_items:
            raw_embedding = item["embedding"]
            if not isinstance(raw_embedding, list) or not raw_embedding:
                raise ValueError("Embedding vector must be a non-empty list.")
            vectors.append([float(value) for value in raw_embedding])
        return vectors
    except (KeyError, TypeError, ValueError) as exc:
        raise ServiceUnavailableError(
            "Invalid embedding vector payload received from AI server."
        ) from exc


def _parse_chat_completion_payload(payload: Any) -> str:
    """OpenAI /v1/chat/completions yanıtından model metnini çıkarır."""
    if not isinstance(payload, dict):
        raise ServiceUnavailableError("Invalid chat completion response format from AI server.")

    if payload.get("error"):
        logger.warning(
            "Chat completion response returned upstream error (%s)",
            _extract_upstream_error_summary(payload.get("error")),
        )
        raise ServiceUnavailableError("AI inference service returned an error.")

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ServiceUnavailableError("Empty choices in chat completion response from AI server.")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ServiceUnavailableError("Invalid choice item in chat completion response.")

    finish_reason = _normalize_finish_reason(first_choice.get("finish_reason"))
    if finish_reason is not None and finish_reason.lower() != "stop":
        raise _build_abnormal_finish_error(finish_reason)

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ServiceUnavailableError("Missing message content in chat completion response.")

    content = message.get("content")
    if not isinstance(content, str):
        raise ServiceUnavailableError("Missing message content in chat completion response.")

    return content


def _extract_sse_delta(raw_line: str) -> _SSEChunk:
    """SSE (Server-Sent Events) satırını ayrıştırır; bitiş/token/finish_reason döner."""
    line = raw_line.strip()
    if not line or not line.startswith("data:"):
        return _SSEChunk()

    data_str = line[5:].strip()
    if data_str == "[DONE]":
        return _SSEChunk(is_done=True)

    try:
        chunk = json.loads(data_str)
    except json.JSONDecodeError as exc:
        raise ServiceUnavailableError("Invalid streaming JSON chunk from AI server.") from exc

    if not isinstance(chunk, dict):
        raise ServiceUnavailableError("Invalid streaming chunk structure from AI server.")

    if chunk.get("error"):
        logger.warning(
            "Upstream LLM SSE stream emitted an error frame (%s)",
            _extract_upstream_error_summary(chunk.get("error")),
        )
        raise ServiceUnavailableError("AI inference service reported an error during streaming.")

    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return _SSEChunk()

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return _SSEChunk()

    finish_reason = _normalize_finish_reason(first_choice.get("finish_reason"))
    delta = first_choice.get("delta")
    token = ""
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str) and content:
            token = content

    return _SSEChunk(token=token, finish_reason=finish_reason)


class AsyncOpenAICompatibleLLMClient(BaseLLMClient):
    """FastAPI katmanı için bağlantı havuzlu (connection-pooled) asenkron AI istemcisi."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "ollama",
        llm_model: str = "llama3.2:3b",
        embedding_model: str = "nomic-embed-text",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._llm_model = llm_model
        self._embedding_model = embedding_model
        self._headers = _build_headers(api_key)
        self._owns_client = client is None
        self._circuit_breaker = circuit_breaker
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
            ),
        )

    async def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        """Metin listesini /v1/embeddings ucu üzerinden toplu vektörlere dönüştürür."""
        if not texts:
            return []
        if self._circuit_breaker is not None:
            return await self._circuit_breaker.call(self._embed_texts_once, texts)
        return await self._embed_texts_once(texts)

    async def _post_json_with_retry(
        self,
        *,
        endpoint: str,
        request_body: dict[str, Any],
        operation_label: str,
        max_retries: int,
        max_wait_seconds: float | None,
        fallback_base_seconds: float,
    ) -> Any:
        """OpenAI-uyumlu JSON uçlarına 429 hız sınırı yeniden denemeli POST isteği gönderir."""
        try:
            for attempt in range(max_retries + 1):
                response = await self._client.post(
                    f"{self._base_url}{endpoint}",
                    headers=self._headers,
                    json=request_body,
                )
                if response.status_code == 429:
                    delay = _extract_retry_delay_seconds(
                        response,
                        attempt,
                        fallback_base_seconds=fallback_base_seconds,
                    )
                    within_budget = max_wait_seconds is None or delay <= max_wait_seconds
                    if attempt < max_retries and within_budget:
                        logger.warning(
                            "%s rate limit (429) hit; retrying in %.1fs (attempt %d/%d)",
                            operation_label,
                            delay,
                            attempt + 1,
                            max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ServiceUnavailableError(
                        "AI inference service rate limit exceeded.",
                        headers={"Retry-After": str(max(1, math.ceil(delay)))},
                    )
                response.raise_for_status()
                return response.json()
        except AppError:
            raise
        except httpx.HTTPError as exc:
            logger.error("%s request to AI server failed: %s", operation_label, exc)
            raise ServiceUnavailableError("AI inference service is currently unavailable.") from exc
        except ValueError as exc:
            raise ServiceUnavailableError("Invalid JSON received from AI server.") from exc

    async def _embed_texts_once(self, texts: Sequence[str]) -> list[list[float]]:
        payload = await self._post_json_with_retry(
            endpoint="/embeddings",
            request_body={
                "model": self._embedding_model,
                "input": list(texts),
            },
            operation_label="Embedding",
            max_retries=_MAX_RATE_LIMIT_RETRIES,
            max_wait_seconds=None,
            fallback_base_seconds=15.0,
        )
        return _parse_embeddings_payload(payload, expected_count=len(texts))

    async def generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        """Mesaj geçmişine göre /v1/chat/completions ucundan tam yanıt üretir."""
        if self._circuit_breaker is not None:
            return await self._circuit_breaker.call(
                self._generate_once,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        return await self._generate_once(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def _generate_once(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> str:
        request_body: dict[str, Any] = {
            "model": self._llm_model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens

        payload = await self._post_json_with_retry(
            endpoint="/chat/completions",
            request_body=request_body,
            operation_label="Chat completion",
            max_retries=_CHAT_MAX_RATE_LIMIT_RETRIES,
            max_wait_seconds=_CHAT_MAX_RETRY_WAIT_SECONDS,
            fallback_base_seconds=1.5,
        )
        return _parse_chat_completion_payload(payload)

    async def stream_generate(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Mesaj geçmişine göre /v1/chat/completions ucundan SSE ile token akışı üretir."""
        if self._circuit_breaker is not None:
            async for token in self._circuit_breaker.stream(
                lambda: self._stream_generate_once(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            ):
                yield token
            return

        async for token in self._stream_generate_once(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield token

    async def _stream_generate_once(
        self,
        *,
        messages: Sequence[ChatMessage],
        temperature: float = 0.0,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        request_body: dict[str, Any] = {
            "model": self._llm_model,
            "messages": [{"role": msg.role, "content": msg.content} for msg in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            request_body["max_tokens"] = max_tokens

        try:
            for attempt in range(_CHAT_MAX_RATE_LIMIT_RETRIES + 1):
                pending_retry_delay: float | None = None
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=request_body,
                ) as response:
                    if response.status_code == 429:
                        await response.aread()
                        delay = _extract_retry_delay_seconds(
                            response,
                            attempt,
                            fallback_base_seconds=1.5,
                        )
                        if (
                            attempt < _CHAT_MAX_RATE_LIMIT_RETRIES
                            and delay <= _CHAT_MAX_RETRY_WAIT_SECONDS
                        ):
                            pending_retry_delay = delay
                        else:
                            raise ServiceUnavailableError(
                                "AI inference service rate limit exceeded.",
                                headers={"Retry-After": str(max(1, math.ceil(delay)))},
                            )
                    else:
                        response.raise_for_status()
                        saw_done = False
                        saw_stop_finish = False
                        emitted_tokens = False
                        async for raw_line in response.aiter_lines():
                            parsed = _extract_sse_delta(raw_line)
                            if parsed.is_done:
                                saw_done = True
                                break
                            if parsed.token:
                                emitted_tokens = True
                                yield parsed.token
                            if parsed.finish_reason is not None:
                                if parsed.finish_reason.lower() == "stop":
                                    saw_stop_finish = True
                                else:
                                    raise _build_abnormal_finish_error(parsed.finish_reason)
                        if not saw_done and not saw_stop_finish:
                            logger.warning(
                                "Upstream LLM stream closed before completion marker "
                                "(emitted_tokens=%s)",
                                emitted_tokens,
                            )
                            raise ServiceUnavailableError(
                                "AI inference stream closed before completion."
                            )
                        return

                if pending_retry_delay is not None:
                    logger.warning(
                        "Streaming chat rate limit (429) hit; retrying in %.1fs (attempt %d/%d)",
                        pending_retry_delay,
                        attempt + 1,
                        _CHAT_MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(pending_retry_delay)
        except AppError:
            raise
        except httpx.HTTPError as exc:
            logger.error("Streaming chat request to AI server failed: %s", exc)
            raise ServiceUnavailableError("AI inference service is currently unavailable.") from exc

    async def close(self) -> None:
        """İstemci bu örnek tarafından oluşturulduysa HTTP bağlantı havuzunu kapatır."""
        if self._owns_client:
            await self._client.aclose()


class SyncOpenAICompatibleLLMClient(SyncBaseLLMClient):
    """Celery worker katmanı için bağlantı havuzlu senkron Embedding istemcisi."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "ollama",
        embedding_model: str = "nomic-embed-text",
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        circuit_breaker: SyncCircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._embedding_model = embedding_model
        self._headers = _build_headers(api_key)
        self._owns_client = client is None
        self._circuit_breaker = circuit_breaker
        self._client = client or httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )

    def embed_texts(self, *, texts: Sequence[str]) -> list[list[float]]:
        """Metin listesini /v1/embeddings ucu üzerinden senkron olarak vektörlere dönüştürür."""
        if not texts:
            return []
        if self._circuit_breaker is not None:
            return self._circuit_breaker.call(self._embed_texts_once, texts)
        return self._embed_texts_once(texts)

    def _embed_texts_once(self, texts: Sequence[str]) -> list[list[float]]:
        payload: Any = None
        try:
            for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
                response = self._client.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers,
                    json={
                        "model": self._embedding_model,
                        "input": list(texts),
                    },
                )
                if response.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                    delay = _extract_retry_delay_seconds(response, attempt)
                    logger.warning(
                        "Sync embedding rate limit (429) hit; retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
                break
        except httpx.HTTPError as exc:
            logger.error("Sync embedding request to AI server failed: %s", exc)
            raise ServiceUnavailableError("AI inference service is currently unavailable.") from exc
        except ValueError as exc:
            raise ServiceUnavailableError("Invalid JSON received from AI server.") from exc

        return _parse_embeddings_payload(payload, expected_count=len(texts))

    def close(self) -> None:
        """İstemci bu örnek tarafından oluşturulduysa senkron HTTP havuzunu kapatır."""
        if self._owns_client:
            self._client.close()
