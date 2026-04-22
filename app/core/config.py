from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Gemini
    gemini_api_key: str = ""
    gemini_model_live: str = "gemini-2.0-flash-live-001"
    gemini_model_text: str = "gemini-2.5-flash"

    # Database
    # Lokal: sqlite+aiosqlite:///./deutschmeister.db
    # Docker / Railway: sqlite+aiosqlite:////app/data/deutschmeister.db (env'den gelir)
    database_url: str = "sqlite+aiosqlite:///./deutschmeister.db"

    # App
    secret_key: str = "change_me_in_production"
    cors_origins: str = "http://localhost:5173"

    # Debug
    debug_ws: bool = False

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",")]


@lru_cache
def get_settings() -> Settings:
    return Settings()
