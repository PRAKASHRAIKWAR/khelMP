from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
ECOSYSTEM_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    app_name: str = "KhelMP API"
    database_url: str = f"sqlite:///{BACKEND_DIR / 'platform.db'}"
    jwt_secret_key: str = ""
    django_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    cors_origins: str = "http://127.0.0.1:8000,http://localhost:8000"
    upload_dir: Path = BACKEND_DIR / "uploads"
    max_upload_bytes: int = 100 * 1024 * 1024
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_model_fallbacks: str = "gemini-flash-latest,gemini-3.7-flash,gemini-3.5-flash,gemini-2.5-flash"
    auto_create_schema: bool = False

    model_config = SettingsConfigDict(
        env_file=ECOSYSTEM_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def signing_secret(self) -> str:
        return self.jwt_secret_key or self.django_secret_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()