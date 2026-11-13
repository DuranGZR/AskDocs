from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
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

    @property
    def max_upload_size_bytes(self) -> int:
        """Maksimum yükleme boyutunu bayt cinsinden döndürür."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

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
