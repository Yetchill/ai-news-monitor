"""AI classification and summarization operations with job tracking."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, cast

from app.classifiers.llm import LLMClassifier
from app.classifiers.providers import (
    LLMConfigError,
    LLMProviderError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleProvider,
)
from app.classifiers.rule_based import RuleBasedClassifier
from app.domain.models import AIJob, IntelligenceItem
from app.services.ai_settings_service import AIConfig, AISettingsService
from app.services.error_sanitization import sanitize_error
from app.storage.database import Database

logger = logging.getLogger(__name__)


class AIOperationService:
    """Perform AI classification and summarization with job tracking."""

    MAX_BATCH_SIZE = 100

    def __init__(self, database: Database) -> None:
        self._database = database
        self._settings = AISettingsService(database)

    # -- Classification --

    async def classify_single(self, item_id: int, trigger: str = "manual") -> AIJob:
        return await self._classify_items([item_id], trigger)

    async def classify_batch(self, item_ids: list[int], trigger: str = "manual") -> AIJob:
        return await self._classify_items(item_ids[: self.MAX_BATCH_SIZE], trigger)

    async def classify_all_unclassified(self, trigger: str = "manual") -> AIJob:
        with self._database.session() as session:
            items = (
                session.query(IntelligenceItem)
                .filter(
                    IntelligenceItem.is_active.is_(True),
                    IntelligenceItem.admission_accepted.is_(True),
                )
                .filter(IntelligenceItem.manual_category.is_(None))
                .limit(self.MAX_BATCH_SIZE)
                .all()
            )
            item_ids = [item.id for item in items]
        if not item_ids:
            job = AIJob(
                job_type="classification",
                trigger=trigger,
                status="completed",
                total_count=0,
                model="",
                finished_at=datetime.now(UTC),
            )
            with self._database.session() as session:
                session.add(job)
                session.commit()
                return job
        return await self._classify_items(item_ids, trigger)

    async def _classify_items(self, item_ids: list[int], trigger: str) -> AIJob:
        config = self._settings.get_config()
        if not config.api_key_configured:
            job = AIJob(
                job_type="classification",
                trigger=trigger,
                status="failed",
                total_count=len(item_ids),
                model="",
                error_summary="未配置 API Key, 请前往 AI 页面配置。",
                finished_at=datetime.now(UTC),
            )
            with self._database.session() as session:
                session.add(job)
                session.commit()
                return job

        job = AIJob(
            job_type="classification",
            trigger=trigger,
            status="running",
            total_count=len(item_ids),
            model=config.model,
            started_at=datetime.now(UTC),
        )
        with self._database.session() as session:
            session.add(job)
            session.commit()
            job_id = job.id

        provider = _build_provider(config)
        classifier = LLMClassifier(provider)
        rule_classifier = RuleBasedClassifier.from_yaml()

        success = 0
        failure = 0
        skipped = 0
        fallback = 0
        errors: list[str] = []

        for item_id in item_ids:
            with self._database.session() as session:
                item = session.get(IntelligenceItem, item_id)
                if item is None:
                    skipped += 1
                    continue
                if item.manual_category is not None:
                    skipped += 1
                    continue
                title = item.title
                summary = item.summary

            try:
                llm_result = await classifier.classify(
                    _make_item(title, summary)
                )
                with self._database.session() as session:
                    item = session.get(IntelligenceItem, item_id)
                    if item is None:
                        skipped += 1
                        continue
                    if item.manual_category is not None:
                        skipped += 1
                        continue
                    if llm_result.category.value != "unclassified":
                        item.category = llm_result.category
                        item.classification_score = llm_result.score
                        item.classification_reason = llm_result.reason
                        item.automatic_category_provider = "llm"
                        success += 1
                    else:
                        fallback += 1
                    session.commit()
            except (LLMTimeoutError, LLMProviderError, LLMResponseError, LLMConfigError) as exc:
                failure += 1
                err_msg = sanitize_error(exc, limit=120)
                errors.append(f"资讯 {item_id}: {err_msg}")
                rule_result = await rule_classifier.classify(_make_item(title, summary))
                with self._database.session() as session:
                    item = session.get(IntelligenceItem, item_id)
                    if item is not None and item.manual_category is None:
                        item.category = rule_result.category
                        item.classification_score = rule_result.score
                        item.classification_reason = rule_result.reason
                        item.automatic_category_provider = "rule_based"
                        fallback += 1
                        session.commit()
            except Exception:
                failure += 1

        job_status = "completed"
        job_errors = "\n".join(errors[:5]) if errors else None
        if failure > 0 and success == 0:
            job_status = "failed"
        elif failure > 0:
            job_status = "partial_failure"

        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job:
                job.status = job_status
                job.success_count = success
                job.failure_count = failure
                job.skipped_count = skipped
                job.fallback_count = fallback
                job.error_summary = sanitize_error(job_errors, limit=300) if job_errors else None
                job.finished_at = datetime.now(UTC)
                session.commit()

        return cast(AIJob, job)

    # -- Summarization --

    async def summarize_single(self, item_id: int, trigger: str = "manual") -> AIJob:
        return await self._summarize_items([item_id], trigger, retry_failed_only=False)

    async def summarize_batch(
        self, item_ids: list[int], trigger: str = "manual", retry_failed_only: bool = False
    ) -> AIJob:
        return await self._summarize_items(
            item_ids[: self.MAX_BATCH_SIZE], trigger, retry_failed_only
        )

    async def summarize_all_unsummarized(self, trigger: str = "manual") -> AIJob:
        with self._database.session() as session:
            items = (
                session.query(IntelligenceItem)
                .filter(
                    IntelligenceItem.is_active.is_(True),
                    IntelligenceItem.admission_accepted.is_(True),
                    IntelligenceItem.ai_summary.is_(None),
                )
                .limit(self.MAX_BATCH_SIZE)
                .all()
            )
            item_ids = [item.id for item in items]
        if not item_ids:
            job = AIJob(
                job_type="summarization",
                trigger=trigger,
                status="completed",
                total_count=0,
                model="",
                finished_at=datetime.now(UTC),
            )
            with self._database.session() as session:
                session.add(job)
                session.commit()
                return job
        return await self._summarize_items(item_ids, trigger, retry_failed_only=False)

    async def _summarize_items(
        self, item_ids: list[int], trigger: str, retry_failed_only: bool
    ) -> AIJob:
        config = self._settings.get_config()
        if not config.api_key_configured:
            job = AIJob(
                job_type="summarization",
                trigger=trigger,
                status="failed",
                total_count=len(item_ids),
                model="",
                error_summary="未配置 API Key, 请前往 AI 页面配置。",
                finished_at=datetime.now(UTC),
            )
            with self._database.session() as session:
                session.add(job)
                session.commit()
                return job

        job = AIJob(
            job_type="summarization",
            trigger=trigger,
            status="running",
            total_count=len(item_ids),
            model=config.model,
            started_at=datetime.now(UTC),
        )
        with self._database.session() as session:
            session.add(job)
            session.commit()
            job_id = job.id

        provider = _build_provider(config)
        success = 0
        failure = 0
        skipped = 0
        errors: list[str] = []

        for item_id in item_ids:
            with self._database.session() as session:
                item = session.get(IntelligenceItem, item_id)
                if item is None:
                    skipped += 1
                    continue
                if item.ai_summary is not None and not retry_failed_only:
                    skipped += 1
                    continue
                title = item.title
                summary = item.summary

            try:
                prompt = _summary_prompt(title, summary)
                client = provider._client()  # pyright: ignore[reportPrivateUsage]
                resp = await client.post(
                    f"{config.base_url.rstrip('/')}/v1/chat/completions",
                    json={
                        "model": config.model,
                        "messages": [
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.3,
                        "max_tokens": 300,
                    },
                )
                body = resp.json()
                choices: list[dict[str, Any]] = body.get("choices", [])
                ai_text = ""
                if choices and choices[0].get("message", {}).get("content"):
                    ai_text = choices[0]["message"]["content"]
                ai_text = ai_text.strip()[:1000]

                with self._database.session() as session:
                    item = session.get(IntelligenceItem, item_id)
                    if item is None:
                        skipped += 1
                        continue
                    item.ai_summary = ai_text
                    item.ai_summary_model = config.model
                    success += 1
                    session.commit()
            except (LLMTimeoutError, LLMProviderError, LLMResponseError, LLMConfigError) as exc:
                failure += 1
                errors.append(f"资讯 {item_id}: {sanitize_error(exc, limit=120)}")
            except Exception:
                failure += 1

            await asyncio.sleep(0.3)

        job_status = "completed"
        if failure > 0 and success == 0:
            job_status = "failed"
        elif failure > 0:
            job_status = "partial_failure"

        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job:
                job.status = job_status
                job.success_count = success
                job.failure_count = failure
                job.skipped_count = skipped
                job.fallback_count = 0
                job.error_summary = (
                    sanitize_error("\n".join(errors[:5]), limit=300) if errors else None
                )
                job.finished_at = datetime.now(UTC)
                session.commit()

        return cast(AIJob, job)

    def get_recent_jobs(self, limit: int = 20) -> list[AIJob]:
        with self._database.session() as session:
            from sqlalchemy import desc

            return list(
                session.query(AIJob)
                .order_by(desc(AIJob.started_at))
                .limit(limit)
                .all()
            )


def _build_provider(config: AIConfig) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout_seconds=config.timeout_seconds,
    )


def _make_item(title: str, summary: str | None):
    from app.domain.collection import CollectedItem

    return CollectedItem(
        title=title,
        summary=summary,
        original_url="",
        canonical_url="",
    )


def _summary_prompt(title: str, summary: str | None) -> str:
    parts = ["请用2-3句简洁中文总结以下 AI 资讯的核心内容.禁止编造,只使用提供的信息。"]
    parts.append(f"标题: {title}")
    if summary:
        parts.append(f"原始摘要: {summary}")
    else:
        parts.append("原始摘要: 无")
    parts.append("\n只输出总结文本,不要加任何前缀说明。")
    return "\n".join(parts)
