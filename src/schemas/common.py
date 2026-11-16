from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """Tek bir API hatasının standart ayrıntı yapısı."""

    code: str
    message: str
    request_id: str = "-"
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """Tüm API hata yanıtlarının ortak zarf (envelope) şeması."""

    error: ErrorDetail


class HealthResponse(BaseModel):
    """Sistem ve bağımlılık sağlık durumunun standart yanıt şeması."""

    status: Literal["ok", "degraded"]
    app_name: str
    version: str
    environment: str
    checks: dict[str, str] = Field(default_factory=dict)
    circuits: dict[str, str] = Field(default_factory=dict)
