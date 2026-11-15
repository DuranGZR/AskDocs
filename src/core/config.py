from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Uygulamanın tüm ortam değişkenlerini doğrulayan merkezi ayar sınıfı."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "CoreRAG-Engine"
    APP_ENV: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["console", "json"] = "console"

    # --- Security ---
    API_KEY: SecretStr = Field(..., min_length=8)

    # --- Storage (Shared Volume) ---
    UPLOAD_DIR: Path = Path("./data/uploads")
    MAX_UPLOAD_SIZE_MB: int = Field(default=100, ge=1, le=2048)
    CHUNK_SIZE: int = Field(default=500, ge=100, le=4000)
    CHUNK_OVERLAP: int = Field(default=75, ge=0, le=1000)

    # --- Redis & Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # --- Vector DB (Qdrant) ---
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_COLLECTION_NAME: str = Field(
        default="documents",
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    QDRANT_VECTOR_SIZE: int = Field(default=768, ge=1, le=8192)
    RAG_TOP_K: int = Field(default=5, ge=1, le=20)
    RAG_SCORE_THRESHOLD: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- AI / LLM & Embedding (Ollama / vLLM / OpenAI-Compatible) ---
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_API_KEY: SecretStr = SecretStr("ollama")
    LLM_MODEL: str = Field(default="llama3.2:3b", min_length=1, max_length=128)
    EMBEDDING_MODEL: str = Field(default="nomic-embed-text", min_length=1, max_length=128)
    EMBEDDING_BATCH_SIZE: int = Field(default=32, ge=1, le=512)
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0.0, le=600.0)

    @property
    def max_upload_size_bytes(self) -> int:
        """Maksimum yükleme boyutunu bayt cinsinden döndürür."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @field_validator("APP_ENV", "LOG_FORMAT", mode="before")
    @classmethod
    def normalize_lowercase_enum(cls, value: object) -> object:
        """Ortam ve log formatı alanlarındaki büyük/küçük harf farklarını normalize eder."""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Log seviyesini büyük harfe normalize eder."""
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("API_KEY")
    @classmethod
    def validate_api_key_strength(cls, value: SecretStr) -> SecretStr:
        """API anahtarının baş/son boşluklar hariç en az 8 karakter olmasını sağlar."""
        stripped = value.get_secret_value().strip()
        if len(stripped) < 8:
            raise ValueError("API_KEY must contain at least 8 non-whitespace characters")
        return SecretStr(stripped)

    @field_validator("REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND")
    @classmethod
    def validate_redis_scheme(cls, value: str) -> str:
        """Redis/Celery URL'lerinin geçerli şema, host ve port yapısını doğrular."""
        cleaned = value.strip()
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError(
                "URL must use 'redis://' or 'rediss://' scheme and include a valid hostname"
            )
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("URL contains an invalid port number") from exc
        return cleaned

    @field_validator("QDRANT_URL", "LLM_BASE_URL")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        """HTTP/HTTPS servis adreslerinin geçerli şema, host ve port yapısını doğrular."""
        cleaned = value.strip().rstrip("/")
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "URL must use 'http://' or 'https://' scheme and include a valid hostname"
            )
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("URL contains an invalid port number") from exc
        return cleaned

    @model_validator(mode="after")
    def validate_production_security(self) -> Self:
        """Üretim ortamında güvensiz varsayılan ayarların kullanılmasını engeller."""
        if (
            self.APP_ENV == "production"
            and self.API_KEY.get_secret_value() == "change-me-in-local-env"
        ):
            raise ValueError("API_KEY must be changed from the example value in production")
        if self.APP_ENV == "production" and self.DEBUG:
            raise ValueError("DEBUG must be False in production")
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        return self


@lru_cache
def get_settings() -> Settings:
    """Ayar nesnesini tek sefer oluşturup önbellekten döndürür."""
    return Settings()
