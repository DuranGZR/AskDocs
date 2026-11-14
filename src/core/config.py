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

    # --- Redis & Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

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
        return self


@lru_cache
def get_settings() -> Settings:
    """Ayar nesnesini tek sefer oluşturup önbellekten döndürür."""
    return Settings()
