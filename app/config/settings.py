"""Typed application settings loaded from environment variables and ``.env``."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "intelligence.db"


class Settings(BaseSettings):
    """Stage-one runtime settings.

    Environment variables use the ``AIM_`` prefix and override values from the
    project-local ``.env`` file.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="AIM_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AI 行业动态与成果申报情报工具"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"
    log_dir: Path = PROJECT_ROOT / "logs"
    log_level: str = "INFO"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    log_backup_count: int = Field(default=5, ge=0)

    classifier_mode: Literal["rule", "llm", "hybrid"] = "rule"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = Field(default=30, ge=1)
    llm_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for the current process."""

    return Settings()
