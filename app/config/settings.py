"""Typed application settings loaded from environment variables and ``.env``."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config.paths import default_data_dir, default_database_url, resource_root

PROJECT_ROOT = resource_root()


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
    data_dir: Path = Field(default_factory=default_data_dir)
    database_url: str = Field(default_factory=default_database_url)
    log_dir: Path = Field(default_factory=lambda: default_data_dir() / "logs")
    output_dir: Path = Field(default_factory=lambda: default_data_dir() / "output")
    log_level: str = "INFO"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    log_backup_count: int = Field(default=5, ge=0)

    classifier_mode: Literal["rule", "llm", "hybrid"] = "rule"
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: int = Field(default=30, ge=1)
    llm_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    http_max_response_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    sqlite_busy_timeout_ms: int = Field(default=10_000, ge=0, le=120_000)

    @model_validator(mode="after")
    def derive_writable_paths(self) -> "Settings":
        """Keep all default writable artifacts under the selected data directory."""

        if "database_url" not in self.model_fields_set:
            self.database_url = f"sqlite:///{(self.data_dir / 'intelligence.db').as_posix()}"
        if "log_dir" not in self.model_fields_set:
            self.log_dir = self.data_dir / "logs"
        if "output_dir" not in self.model_fields_set:
            self.output_dir = self.data_dir / "output"
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings instance for the current process."""

    return Settings()
