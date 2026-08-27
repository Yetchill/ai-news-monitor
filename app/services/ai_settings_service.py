"""Independent AI provider settings and global workflow selection."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.models import AIProviderSetting, AISettings
from app.storage.database import Database

PROVIDER_DEFAULTS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
    "openai": ("OpenAI", "https://api.openai.com", "gpt-4.1-mini"),
    "openrouter": (
        "OpenRouter",
        "https://openrouter.ai/api",
        "openai/gpt-4.1-mini",
    ),
    "custom": ("自定义 OpenAI 兼容服务", "", ""),
}


@dataclass(slots=True)
class AIConfig:
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key: str = ""
    timeout_seconds: int = 30
    max_retries: int = 1
    enabled: bool = True
    classifier_mode: str = "off"
    summarizer_mode: str = "off"
    classification_provider: str = "deepseek"
    classification_model: str = "deepseek-chat"
    summarization_provider: str = "deepseek"
    summarization_model: str = "deepseek-chat"
    classification_batch_mode: str = "smart"

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
    def provider_label(self) -> str:
        return provider_label(self.provider)

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

    def safe_provider_data(self) -> dict[str, object]:
        """Return browser-safe provider data without a credential or key fragment."""

        return {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "base_url": self.base_url,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "enabled": self.enabled,
            "api_key_configured": self.api_key_configured,
        }


class AISettingsService:
    """Read and write isolated provider records and workflow preferences."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get_config(self, provider: str | None = None) -> AIConfig:
        from app.config.settings import get_settings

        env = get_settings()
        with self._database.session() as session:
            workflow = session.get(AISettings, 1)
            self._migrate_legacy_key(session, workflow)
            selected = provider or (
                workflow.selected_provider if workflow is not None else "deepseek"
            )
            selected = selected if selected in PROVIDER_DEFAULTS else "custom"
            provider_row = session.get(AIProviderSetting, selected)

            _, default_url, default_model = PROVIDER_DEFAULTS[selected]
            env_key = env.llm_api_key if selected == "deepseek" and provider_row is None else ""
            config = AIConfig(
                provider=selected,
                base_url=(
                    provider_row.base_url
                    if provider_row is not None
                    else (env.llm_base_url if selected == "deepseek" else default_url)
                ),
                model=(
                    provider_row.model
                    if provider_row is not None
                    else (env.llm_model if selected == "deepseek" else default_model)
                ),
                api_key=provider_row.api_key if provider_row is not None else env_key,
                timeout_seconds=(
                    provider_row.timeout_seconds
                    if provider_row is not None
                    else (env.llm_timeout_seconds if selected == "deepseek" else 30)
                ),
                max_retries=provider_row.max_retries if provider_row is not None else 1,
                enabled=provider_row.enabled if provider_row is not None else True,
                classifier_mode=(
                    workflow.classifier_mode
                    if workflow is not None
                    else self._map_mode(env.classifier_mode)
                ),
                summarizer_mode=workflow.summarizer_mode if workflow is not None else "off",
                classification_provider=(
                    workflow.classification_provider if workflow is not None else "deepseek"
                ),
                classification_model=(
                    workflow.classification_model if workflow is not None else env.llm_model
                ),
                summarization_provider=(
                    workflow.summarization_provider if workflow is not None else "deepseek"
                ),
                summarization_model=(
                    workflow.summarization_model if workflow is not None else env.llm_model
                ),
                classification_batch_mode=(
                    workflow.classification_batch_mode if workflow is not None else "smart"
                ),
            )
        return config

    def get_operation_config(self, operation: str) -> AIConfig:
        workflow = self.get_config()
        if operation == "classification":
            provider = workflow.classification_provider
            model = workflow.classification_model
        elif operation == "summarization":
            provider = workflow.summarization_provider
            model = workflow.summarization_model
        else:
            raise ValueError("未知 AI 操作类型")
        config = self.get_config(provider)
        config.model = model or config.model
        return config

    def list_provider_configs(self) -> list[AIConfig]:
        return [self.get_config(provider) for provider in PROVIDER_DEFAULTS]

    def save(self, config: AIConfig) -> None:
        if config.provider not in PROVIDER_DEFAULTS:
            raise ValueError("不支持的 AI 供应商")
        if config.classification_provider not in PROVIDER_DEFAULTS:
            raise ValueError("分类供应商无效")
        if config.summarization_provider not in PROVIDER_DEFAULTS:
            raise ValueError("总结供应商无效")
        if config.classification_batch_mode not in {"quick", "smart", "precise"}:
            raise ValueError("分类模式无效")

        now = datetime.now(UTC)
        with self._database.session() as session:
            workflow = session.get(AISettings, 1)
            if workflow is None:
                workflow = AISettings(id=1)
                session.add(workflow)
            self._migrate_legacy_key(session, workflow)

            provider_row = session.get(AIProviderSetting, config.provider)
            if provider_row is None:
                provider_row = AIProviderSetting(
                    provider=config.provider,
                    base_url=config.base_url,
                    model=config.model,
                )
                session.add(provider_row)
            provider_row.base_url = config.base_url
            provider_row.model = config.model
            if config.api_key:
                provider_row.api_key = config.api_key
            provider_row.timeout_seconds = config.timeout_seconds
            provider_row.max_retries = config.max_retries
            provider_row.enabled = config.enabled
            provider_row.updated_at = now

            workflow.selected_provider = config.provider
            workflow.classifier_mode = config.classifier_mode
            workflow.summarizer_mode = config.summarizer_mode
            workflow.classification_provider = config.classification_provider
            workflow.classification_model = config.classification_model
            workflow.summarization_provider = config.summarization_provider
            workflow.summarization_model = config.summarization_model
            workflow.classification_batch_mode = config.classification_batch_mode
            workflow.api_key = ""
            workflow.updated_at = now
            session.commit()

    def clear_key(self, provider: str) -> None:
        if provider not in PROVIDER_DEFAULTS:
            raise ValueError("不支持的 AI 供应商")
        with self._database.session() as session:
            row = session.get(AIProviderSetting, provider)
            if row is not None:
                row.api_key = ""
                row.updated_at = datetime.now(UTC)
                session.commit()

    @staticmethod
    def _migrate_legacy_key(session: object, workflow: AISettings | None) -> None:
        """Move a pre-Stage-2 singleton key exactly once.

        This also covers databases created from metadata in tests, where the
        Alembic data-copy step is not executed.
        """

        if workflow is None or not workflow.api_key:
            return
        from sqlalchemy.orm import Session

        typed_session = session
        if not isinstance(typed_session, Session):
            return
        provider = workflow.provider or "deepseek"
        row = typed_session.get(AIProviderSetting, provider)
        if row is None:
            row = AIProviderSetting(
                provider=provider,
                base_url=workflow.base_url,
                model=workflow.model,
                api_key=workflow.api_key,
                timeout_seconds=workflow.timeout_seconds,
                max_retries=workflow.max_retries,
                enabled=True,
            )
            typed_session.add(row)
        elif not row.api_key:
            row.api_key = workflow.api_key
        workflow.selected_provider = provider
        workflow.classification_provider = provider
        workflow.classification_model = workflow.model
        workflow.summarization_provider = provider
        workflow.summarization_model = workflow.model
        workflow.api_key = ""
        typed_session.flush()

    @staticmethod
    def _map_mode(env_mode: str) -> str:
        if env_mode == "rule":
            return "off"
        if env_mode in ("llm", "hybrid"):
            return "auto"
        return "off"


def provider_label(provider: str) -> str:
    preset = PROVIDER_DEFAULTS.get(provider)
    return preset[0] if preset else provider
