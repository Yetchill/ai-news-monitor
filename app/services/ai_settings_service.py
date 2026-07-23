"""AI settings persistence and runtime configuration."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.models import AISettings
from app.storage.database import Database


@dataclass
class AIConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""
    timeout_seconds: int = 30
    max_retries: int = 1
    classifier_mode: str = "off"
    classifier_strategy: str = "hybrid"
    summarizer_mode: str = "off"

    @property
    def api_key_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def masked_key(self) -> str:
        key = self.api_key
        if not key:
            return ""
        if len(key) < 7:
            return "****"
        return key[:3] + "****" + key[-4:]

    @property
    def class_mode_label(self) -> str:
        return {"off": "关闭", "manual": "手动", "auto": "自动参与更新"}.get(
            self.classifier_mode, self.classifier_mode
        )

    @property
    def summary_mode_label(self) -> str:
        return {"off": "关闭", "manual": "手动", "auto": "自动参与更新"}.get(
            self.summarizer_mode, self.summarizer_mode
        )


class AISettingsService:
    """Read and write AI settings with env-var fallback."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get_config(self) -> AIConfig:
        from app.config.settings import get_settings

        env = get_settings()
        with self._database.session() as session:
            row = session.get(AISettings, 1)
        if row is None:
            return AIConfig(
                provider="deepseek",
                base_url=env.llm_base_url,
                model=env.llm_model,
                api_key=env.llm_api_key,
                timeout_seconds=env.llm_timeout_seconds,
                classifier_mode=self._map_mode(env.classifier_mode),
            )
        return AIConfig(
            provider=row.provider or "deepseek",
            base_url=row.base_url or env.llm_base_url,
            model=row.model or env.llm_model,
            api_key=row.api_key,
            timeout_seconds=row.timeout_seconds or env.llm_timeout_seconds,
            max_retries=row.max_retries,
            classifier_mode=row.classifier_mode or "off",
            classifier_strategy=row.classifier_strategy or "hybrid",
            summarizer_mode=row.summarizer_mode or "off",
        )

    def save(self, config: AIConfig) -> None:
        with self._database.session() as session:
            row = session.get(AISettings, 1)
            if row is None:
                row = AISettings(id=1)
                session.add(row)
            row.provider = config.provider
            row.base_url = config.base_url
            row.model = config.model
            if config.api_key:
                row.api_key = config.api_key
            row.timeout_seconds = config.timeout_seconds
            row.max_retries = config.max_retries
            row.classifier_mode = config.classifier_mode
            row.classifier_strategy = config.classifier_strategy
            row.summarizer_mode = config.summarizer_mode
            row.updated_at = datetime.now(UTC)
            session.commit()

    def clear_key(self) -> None:
        with self._database.session() as session:
            row = session.get(AISettings, 1)
            if row is not None:
                row.api_key = ""
                row.updated_at = datetime.now(UTC)
                session.commit()

    @staticmethod
    def _map_mode(env_mode: str) -> str:
        if env_mode == "rule":
            return "off"
        if env_mode in ("llm", "hybrid"):
            return "auto"
        return "off"
