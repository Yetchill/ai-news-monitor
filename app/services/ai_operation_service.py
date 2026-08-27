"""Batch AI classification, summarization, and durable job progress."""

import asyncio
import hashlib
import inspect
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc

from app.classifiers.providers import (
    BatchClassificationItem,
    BatchClassificationResult,
    BatchParseResult,
    LLMConfigError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseError,
    OpenAICompatibleProvider,
)
from app.domain.enums import Category
from app.domain.models import AIJob, IntelligenceItem
from app.services.ai_settings_service import AIConfig, AISettingsService
from app.services.error_sanitization import sanitize_error
from app.storage.database import Database

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClassificationTask:
    job_id: int
    item_ids: tuple[int, ...]
    mode: str
    reclassify: bool


@dataclass(frozen=True, slots=True)
class SummarizationTask:
    job_id: int
    item_ids: tuple[int, ...]
    retry_failed_only: bool


@dataclass(slots=True)
class ClassificationRequestStats:
    max_requests: int
    model_requests: int = 0
    retries: int = 0
    parse_failures: int = 0
    splits: int = 0

    def reserve_request(self) -> bool:
        if self.model_requests >= self.max_requests:
            return False
        self.model_requests += 1
        return True


class AIOperationService:
    """Perform AI work without fetching original article pages."""

    MAX_BATCH_SIZE = 100
    MODEL_BATCH_SIZE = 15
    MAX_BATCH_CONCURRENCY = 3
    SMART_CONFIDENCE_THRESHOLD = 0.75
    MAX_SPLIT_DEPTH = 4
    MAX_MODEL_REQUESTS = 50
    ACTIVE_STATUSES = ("pending", "running")

    def __init__(self, database: Database) -> None:
        self._database = database
        self._settings = AISettingsService(database)
        self._active_providers: set[OpenAICompatibleProvider] = set()

    async def aclose(self) -> None:
        providers = tuple(self._active_providers)
        self._active_providers.clear()
        close_calls: list[Awaitable[object]] = []
        for provider in providers:
            close = getattr(provider, "aclose", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    close_calls.append(result)
        if close_calls:
            await asyncio.gather(*close_calls, return_exceptions=True)

    # -- Classification queue and execution --

    async def classify_single(
        self,
        item_id: int,
        trigger: str = "manual",
        *,
        mode: str | None = None,
        reclassify: bool = False,
    ) -> AIJob:
        task, created = self.enqueue_classification(
            [item_id], trigger=trigger, mode=mode, reclassify=reclassify
        )
        if created:
            await self.run_classification_task(task)
        return self.get_job(task.job_id)

    async def classify_batch(
        self,
        item_ids: list[int],
        trigger: str = "manual",
        *,
        mode: str | None = None,
        reclassify: bool = False,
    ) -> AIJob:
        task, created = self.enqueue_classification(
            item_ids[: self.MAX_BATCH_SIZE],
            trigger=trigger,
            mode=mode,
            reclassify=reclassify,
        )
        if created:
            await self.run_classification_task(task)
        return self.get_job(task.job_id)

    async def classify_all_unclassified(self, trigger: str = "manual") -> AIJob:
        task, created = self.enqueue_classification(
            None, trigger=trigger, mode=None, reclassify=False
        )
        if created:
            await self.run_classification_task(task)
        return self.get_job(task.job_id)

    def enqueue_classification(
        self,
        item_ids: list[int] | None,
        *,
        trigger: str,
        mode: str | None,
        reclassify: bool,
    ) -> tuple[ClassificationTask, bool]:
        workflow = self._settings.get_config()
        selected_mode = mode or workflow.classification_batch_mode or "smart"
        if selected_mode not in {"quick", "smart", "precise"}:
            raise ValueError("分类模式无效")
        resolved_ids = self._classification_ids(item_ids, reclassify=reclassify)
        signature = _request_signature("classification", resolved_ids, selected_mode, reclassify)
        config = self._settings.get_operation_config("classification")
        task, created = self._create_classification_job(
            resolved_ids,
            trigger=trigger,
            mode=selected_mode,
            signature=signature,
            config=config,
            reclassify=reclassify,
        )
        return task, created

    def _create_classification_job(
        self,
        item_ids: list[int],
        *,
        trigger: str,
        mode: str,
        signature: str,
        config: AIConfig,
        reclassify: bool,
    ) -> tuple[ClassificationTask, bool]:
        with self._database.session() as session:
            duplicate = (
                session.query(AIJob)
                .filter(
                    AIJob.job_type == "classification",
                    AIJob.status.in_(self.ACTIVE_STATUSES),
                )
                .order_by(desc(AIJob.id))
                .first()
            )
            if duplicate is not None:
                return (
                    ClassificationTask(duplicate.id, tuple(item_ids), mode, reclassify),
                    False,
                )
            now = datetime.now(UTC)
            if not item_ids:
                status = "completed"
                finished_at = now
                error_summary = None
            elif not config.enabled:
                status = "failed"
                finished_at = now
                error_summary = "当前分类供应商未启用。"
            elif not config.api_key_configured:
                status = "failed"
                finished_at = now
                error_summary = "当前分类供应商未配置 API Key。"
            else:
                status = "pending"
                finished_at = None
                error_summary = None
            job = AIJob(
                job_type="classification",
                trigger=trigger,
                status=status,
                total_count=len(item_ids),
                provider=config.provider,
                model=config.model,
                classification_mode=mode,
                request_signature=signature,
                error_summary=error_summary,
                finished_at=finished_at,
            )
            session.add(job)
            session.commit()
            return ClassificationTask(job.id, tuple(item_ids), mode, reclassify), True

    async def run_classification_task(self, task: ClassificationTask) -> None:
        job = self.get_job(task.job_id)
        if job.status != "pending":
            return
        config = self._settings.get_operation_config("classification")
        provider = _build_provider(config)
        self._active_providers.add(provider)
        stats = ClassificationRequestStats(self.MAX_MODEL_REQUESTS)
        self._mark_running(job.id)

        inputs, initial_skipped = self._load_classification_inputs(
            list(task.item_ids), reclassify=task.reclassify
        )
        success = 0
        failure = 0
        skipped = initial_skipped
        errors: list[str] = []
        batches = _chunks(inputs, self.MODEL_BATCH_SIZE)
        self._update_job(
            job.id,
            total_batches=len(batches),
            skipped_count=skipped,
            processed_count=skipped,
        )

        try:
            first_include_summaries = task.mode == "precise"
            first_results, first_failures = await self._run_classification_round(
                job.id,
                provider,
                batches,
                include_summaries=first_include_summaries,
                max_retries=config.max_retries,
                progress_offset=0,
                progress_kind="smart_first" if task.mode == "smart" else "final",
                output_mode=task.mode,
                stats=stats,
            )

            if task.mode == "smart":
                inputs_by_id = {item.id: item for item in inputs}
                review_ids = {
                    item_id
                    for item_id, result in first_results.items()
                    if _needs_smart_review(result, inputs_by_id[item_id])
                }
                review_ids.update(
                    item_id for item_id in first_failures if inputs_by_id[item_id].summary
                )
                unreviewed_failures = {
                    item_id: reason
                    for item_id, reason in first_failures.items()
                    if item_id not in review_ids
                }
                high_results = {
                    item_id: result
                    for item_id, result in first_results.items()
                    if item_id not in review_ids
                }
                saved, newly_skipped = self._save_classification_results(
                    high_results, mode=task.mode
                )
                success += saved
                skipped += newly_skipped
                self._update_job(
                    job.id,
                    success_count=success,
                    skipped_count=skipped,
                    processed_count=success + failure + skipped,
                )

                review_inputs = [item for item in inputs if item.id in review_ids]
                second_batches = _chunks(review_inputs, self.MODEL_BATCH_SIZE)
                self._update_job(job.id, total_batches=len(batches) + len(second_batches))
                second_results, second_failures = await self._run_classification_round(
                    job.id,
                    provider,
                    second_batches,
                    include_summaries=True,
                    max_retries=config.max_retries,
                    progress_offset=len(batches),
                    progress_kind="final",
                    output_mode="precise",
                    stats=stats,
                )
                saved, newly_skipped = self._save_classification_results(
                    second_results, mode="precise"
                )
                success += saved
                skipped += newly_skipped
                final_failed = set(second_failures) | set(unreviewed_failures)
                final_failed.update(
                    item_id
                    for item_id, result in second_results.items()
                    if result.category is Category.UNCLASSIFIED
                )
                failure += len(final_failed)
                errors.extend(
                    f"资讯 {item_id}: "
                    f"{second_failures.get(item_id, unreviewed_failures.get(item_id, '仍无法确定分类'))}"  # noqa: E501
                    for item_id in sorted(final_failed)
                )
            else:
                saved, newly_skipped = self._save_classification_results(
                    first_results, mode=task.mode
                )
                success += saved
                skipped += newly_skipped
                final_failed = set(first_failures)
                final_failed.update(
                    item_id
                    for item_id, result in first_results.items()
                    if result.category is Category.UNCLASSIFIED
                )
                failure += len(final_failed)
                errors.extend(
                    f"资讯 {item_id}: {first_failures.get(item_id, '无法确定分类')}"
                    for item_id in sorted(final_failed)
                )
        except Exception as exc:
            logger.exception("AI batch classification job failed")
            unresolved = max(0, len(inputs) - success - failure - skipped)
            failure += unresolved
            errors.append(sanitize_error(exc, limit=120))

        self._finish_job(
            job.id,
            success=success,
            failure=failure,
            skipped=skipped,
            errors=errors,
            stats=stats,
        )
        await self._release_provider(provider)

    async def _run_classification_round(
        self,
        job_id: int,
        provider: OpenAICompatibleProvider,
        batches: list[list[BatchClassificationItem]],
        *,
        include_summaries: bool,
        max_retries: int,
        progress_offset: int,
        progress_kind: str,
        output_mode: str,
        stats: ClassificationRequestStats,
    ) -> tuple[dict[str, BatchClassificationResult], dict[str, str]]:
        if not batches:
            return {}, {}
        semaphore = asyncio.Semaphore(self.MAX_BATCH_CONCURRENCY)

        async def run_one(
            batch: list[BatchClassificationItem],
        ) -> tuple[list[BatchClassificationItem], BatchParseResult]:
            async with semaphore:
                result = await self._classify_batch_resilient(
                    provider,
                    batch,
                    include_summaries=include_summaries,
                    max_retries=max_retries,
                    output_mode=output_mode,
                    stats=stats,
                    depth=0,
                )
                return batch, result

        tasks = [asyncio.create_task(run_one(batch)) for batch in batches]
        results: dict[str, BatchClassificationResult] = {}
        failures: dict[str, str] = {}
        completed = 0
        try:
            for future in asyncio.as_completed(tasks):
                _batch, parsed = await future
                completed += 1
                results.update(parsed.results)
                failures.update(parsed.failures)
                self._record_batch_progress(
                    job_id,
                    current_batch=progress_offset + completed,
                    parsed=parsed,
                    progress_kind=progress_kind,
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return results, failures

    async def _classify_batch_resilient(
        self,
        provider: OpenAICompatibleProvider,
        batch: list[BatchClassificationItem],
        *,
        include_summaries: bool,
        max_retries: int,
        output_mode: str,
        stats: ClassificationRequestStats,
        depth: int,
    ) -> BatchParseResult:
        try:
            for attempt in range(max_retries + 1):
                try:
                    if not stats.reserve_request():
                        logger.error(
                            "AI classification request cap reached (%s)", stats.max_requests
                        )
                        return BatchParseResult(
                            {}, {item.id: "任务已达到模型请求次数上限" for item in batch}
                        )
                    return await _call_batch_provider(
                        provider,
                        batch,
                        include_summaries=include_summaries,
                        output_mode=output_mode,
                    )
                except LLMRateLimitError:
                    if attempt >= max_retries:
                        raise
                    stats.retries += 1
                    logger.warning(
                        "AI batch retry after rate limit: attempt=%s size=%s",
                        attempt + 1,
                        len(batch),
                    )
                    await asyncio.sleep(min(2**attempt, 8))
        except LLMResponseError as exc:
            stats.parse_failures += 1
            logger.warning(
                "AI batch parse failure: depth=%s size=%s reason=%s",
                depth,
                len(batch),
                sanitize_error(exc, limit=100),
            )
            if len(batch) > 1 and depth < self.MAX_SPLIT_DEPTH:
                stats.splits += 1
                midpoint = len(batch) // 2
                left, right = await asyncio.gather(
                    self._classify_batch_resilient(
                        provider,
                        batch[:midpoint],
                        include_summaries=include_summaries,
                        max_retries=max_retries,
                        output_mode=output_mode,
                        stats=stats,
                        depth=depth + 1,
                    ),
                    self._classify_batch_resilient(
                        provider,
                        batch[midpoint:],
                        include_summaries=include_summaries,
                        max_retries=max_retries,
                        output_mode=output_mode,
                        stats=stats,
                        depth=depth + 1,
                    ),
                )
                return _merge_parse_results(left, right)
            reason = (
                "批量解析失败且达到最大拆分深度"
                if len(batch) > 1
                else sanitize_error(exc, limit=100)
            )
            return BatchParseResult({}, {item.id: reason for item in batch})
        except (LLMProviderError, LLMConfigError) as exc:
            message = sanitize_error(exc, limit=100)
            return BatchParseResult({}, {item.id: message for item in batch})
        return BatchParseResult({}, {item.id: "批量请求失败" for item in batch})

    def _load_classification_inputs(
        self, item_ids: list[int], *, reclassify: bool
    ) -> tuple[list[BatchClassificationItem], int]:
        result: list[BatchClassificationItem] = []
        skipped = 0
        with self._database.session() as session:
            rows = session.query(IntelligenceItem).filter(IntelligenceItem.id.in_(item_ids)).all()
            by_id = {item.id: item for item in rows}
            for item_id in item_ids:
                item = by_id.get(item_id)
                if (
                    item is None
                    or item.manual_category is not None
                    or (not reclassify and item.category is not Category.UNCLASSIFIED)
                ):
                    skipped += 1
                    continue
                result.append(
                    BatchClassificationItem(
                        id=str(item.id),
                        title=item.title[:1000],
                        summary=_classification_context(item),
                        source_name=item.source.name,
                        source_role=item.source.source_role.value,
                    )
                )
        return result, skipped

    def _save_classification_results(
        self, results: dict[str, BatchClassificationResult], *, mode: str
    ) -> tuple[int, int]:
        success = 0
        skipped = 0
        with self._database.session() as session:
            for item_id, result in results.items():
                item = session.get(IntelligenceItem, int(item_id))
                if item is None or item.manual_category is not None:
                    skipped += 1
                    continue
                if result.category is Category.UNCLASSIFIED:
                    continue
                item.category = result.category
                item.classification_score = None if mode == "quick" else result.confidence * 10
                detail = f"。{result.reason}" if result.reason else ""
                item.classification_reason = (
                    "AI 快速批量分类"
                    if mode == "quick"
                    else f"AI 批量分类, 置信度 {result.confidence:.2f}{detail}"
                )
                item.automatic_category_provider = "llm"
                success += 1
            session.commit()
        return success, skipped

    def _classification_ids(self, requested: list[int] | None, *, reclassify: bool) -> list[int]:
        with self._database.session() as session:
            query = session.query(IntelligenceItem.id).filter(
                IntelligenceItem.is_active.is_(True),
                IntelligenceItem.admission_accepted.is_(True),
                IntelligenceItem.manual_category.is_(None),
            )
            if requested is not None:
                unique_ids = list(dict.fromkeys(requested))[: self.MAX_BATCH_SIZE]
                if not unique_ids:
                    return []
                query = query.filter(IntelligenceItem.id.in_(unique_ids))
            if not reclassify:
                query = query.filter(IntelligenceItem.category == Category.UNCLASSIFIED)
            found = {row[0] for row in query.limit(self.MAX_BATCH_SIZE).all()}
        if requested is None:
            return sorted(found)
        return [item_id for item_id in dict.fromkeys(requested) if item_id in found]

    # -- Summarization --

    async def summarize_single(self, item_id: int, trigger: str = "manual") -> AIJob:
        return await self.summarize_batch([item_id], trigger=trigger)

    async def summarize_batch(
        self,
        item_ids: list[int],
        trigger: str = "manual",
        retry_failed_only: bool = False,
    ) -> AIJob:
        task, created = self.enqueue_summarization(
            item_ids[: self.MAX_BATCH_SIZE],
            trigger=trigger,
            retry_failed_only=retry_failed_only,
        )
        if created:
            await self.run_summarization_task(task)
        return self.get_job(task.job_id)

    async def summarize_all_unsummarized(self, trigger: str = "manual") -> AIJob:
        task, created = self.enqueue_summarization(None, trigger=trigger, retry_failed_only=False)
        if created:
            await self.run_summarization_task(task)
        return self.get_job(task.job_id)

    def enqueue_summarization(
        self,
        item_ids: list[int] | None,
        *,
        trigger: str,
        retry_failed_only: bool,
    ) -> tuple[SummarizationTask, bool]:
        ids = self._summarization_ids(item_ids, retry_failed_only=retry_failed_only)
        signature = _request_signature("summarization", ids, "", retry_failed_only)
        config = self._settings.get_operation_config("summarization")
        with self._database.session() as session:
            duplicate = (
                session.query(AIJob)
                .filter(
                    AIJob.job_type == "summarization",
                    AIJob.status.in_(self.ACTIVE_STATUSES),
                )
                .order_by(desc(AIJob.id))
                .first()
            )
            if duplicate is not None:
                return (
                    SummarizationTask(duplicate.id, tuple(ids), retry_failed_only),
                    False,
                )
            now = datetime.now(UTC)
            runnable = bool(ids) and config.enabled and config.api_key_configured
            job = AIJob(
                job_type="summarization",
                trigger=trigger,
                status="pending" if runnable else ("completed" if not ids else "failed"),
                total_count=len(ids),
                total_batches=len(ids),
                provider=config.provider,
                model=config.model,
                request_signature=signature,
                error_summary=(
                    None if runnable or not ids else "当前总结供应商未启用或未配置 API Key。"
                ),
                finished_at=None if runnable else now,
            )
            session.add(job)
            session.commit()
            return SummarizationTask(job.id, tuple(ids), retry_failed_only), True

    async def run_summarization_task(self, task: SummarizationTask) -> None:
        job = self.get_job(task.job_id)
        if job.status != "pending":
            return
        config = self._settings.get_operation_config("summarization")
        provider = _build_provider(config)
        self._active_providers.add(provider)
        self._mark_running(job.id)
        success = failure = skipped = 0
        errors: list[str] = []
        for index, item_id in enumerate(task.item_ids, start=1):
            with self._database.session() as session:
                item = session.get(IntelligenceItem, item_id)
                if (
                    item is None
                    or (item.ai_summary is not None and not task.retry_failed_only)
                    or not item.summary
                ):
                    skipped += 1
                    self._update_job(
                        job.id,
                        current_batch=index,
                        processed_count=index,
                        skipped_count=skipped,
                    )
                    continue
                title, summary = item.title, item.summary
            try:
                text = await provider.complete(_summary_prompt(title, summary))
                with self._database.session() as session:
                    item = session.get(IntelligenceItem, item_id)
                    if item is None:
                        skipped += 1
                    else:
                        item.ai_summary = text.strip()[:1000]
                        item.ai_summary_model = config.model
                        success += 1
                        session.commit()
            except LLMProviderError as exc:
                failure += 1
                errors.append(f"资讯 {item_id}: {sanitize_error(exc, limit=100)}")
            self._update_job(
                job.id,
                current_batch=index,
                processed_count=index,
                success_count=success,
                failure_count=failure,
                skipped_count=skipped,
            )
        self._finish_job(
            job.id,
            success=success,
            failure=failure,
            skipped=skipped,
            errors=errors,
        )
        await self._release_provider(provider)

    def _summarization_ids(
        self, requested: list[int] | None, *, retry_failed_only: bool
    ) -> list[int]:
        with self._database.session() as session:
            query = session.query(IntelligenceItem.id).filter(
                IntelligenceItem.is_active.is_(True),
                IntelligenceItem.admission_accepted.is_(True),
            )
            if requested is not None:
                unique = list(dict.fromkeys(requested))[: self.MAX_BATCH_SIZE]
                if not unique:
                    return []
                query = query.filter(IntelligenceItem.id.in_(unique))
            if not retry_failed_only:
                query = query.filter(IntelligenceItem.ai_summary.is_(None))
            found = {row[0] for row in query.limit(self.MAX_BATCH_SIZE).all()}
        if requested is None:
            return sorted(found)
        return [item_id for item_id in dict.fromkeys(requested) if item_id in found]

    # -- Job state --

    def recover_interrupted_jobs(self) -> int:
        """Mark work orphaned by a previous process as failed on startup."""

        with self._database.session() as session:
            jobs = session.query(AIJob).filter(AIJob.status.in_(self.ACTIVE_STATUSES)).all()
            now = datetime.now(UTC)
            for job in jobs:
                job.status = "failed"
                job.error_summary = "服务重启, 任务已中断。"
                job.finished_at = now
            session.commit()
            return len(jobs)

    def get_recent_jobs(self, limit: int = 20) -> list[AIJob]:
        with self._database.session() as session:
            return list(
                session.query(AIJob)
                .order_by(desc(AIJob.started_at), desc(AIJob.id))
                .limit(limit)
                .all()
            )

    def get_active_job(self) -> AIJob | None:
        with self._database.session() as session:
            return (
                session.query(AIJob)
                .filter(AIJob.status.in_(self.ACTIVE_STATUSES))
                .order_by(desc(AIJob.id))
                .first()
            )

    def get_job(self, job_id: int) -> AIJob:
        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job is None:
                raise ValueError("AI 任务不存在")
            return job

    def job_data(self, job: AIJob) -> dict[str, object]:
        processed = min(
            job.total_count,
            max(
                job.processed_count,
                job.success_count + job.failure_count + job.skipped_count,
            ),
        )
        percent = 100 if job.total_count == 0 else round(processed * 100 / job.total_count)
        elapsed_ms = _elapsed_ms(job.started_at, job.finished_at)
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "status_label": {
                "pending": "等待中",
                "running": "运行中",
                "completed": "已完成",
                "partial_failure": "部分失败",
                "failed": "失败",
            }.get(job.status, "未知"),
            "total_count": job.total_count,
            "processed_count": processed,
            "success_count": job.success_count,
            "failure_count": job.failure_count,
            "skipped_count": job.skipped_count,
            "provider": job.provider,
            "provider_label": self._settings.get_config(job.provider).provider_label,
            "model": job.model,
            "current_batch": job.current_batch,
            "total_batches": job.total_batches,
            "percentage": percent,
            "classification_mode": job.classification_mode,
            "error_summary": sanitize_error(job.error_summary, limit=300)
            if job.error_summary
            else None,
            "model_request_count": job.model_request_count,
            "retry_count": job.retry_count,
            "parse_failure_count": job.parse_failure_count,
            "split_count": job.split_count,
            "elapsed_ms": elapsed_ms,
            "average_ms_per_item": (
                round(elapsed_ms / job.total_count)
                if elapsed_ms is not None and job.total_count
                else None
            ),
        }

    async def _release_provider(self, provider: OpenAICompatibleProvider) -> None:
        self._active_providers.discard(provider)
        close = getattr(provider, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    def _mark_running(self, job_id: int) -> None:
        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job is not None:
                job.status = "running"
                job.started_at = datetime.now(UTC)
                session.commit()

    def _update_job(self, job_id: int, **values: int) -> None:
        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job is None:
                return
            for name, value in values.items():
                setattr(job, name, value)
            session.commit()

    def _record_batch_progress(
        self,
        job_id: int,
        *,
        current_batch: int,
        parsed: BatchParseResult,
        progress_kind: str,
    ) -> None:
        if progress_kind == "smart_first":
            batch_success = sum(
                result.confidence >= self.SMART_CONFIDENCE_THRESHOLD
                and result.category is not Category.UNCLASSIFIED
                for result in parsed.results.values()
            )
            batch_failure = 0
        else:
            batch_success = sum(
                result.category is not Category.UNCLASSIFIED for result in parsed.results.values()
            )
            batch_failure = len(parsed.failures) + sum(
                result.category is Category.UNCLASSIFIED for result in parsed.results.values()
            )
        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job is None:
                return
            job.current_batch = current_batch
            job.success_count += batch_success
            job.failure_count += batch_failure
            job.processed_count = min(
                job.total_count,
                job.processed_count + batch_success + batch_failure,
            )
            session.commit()

    def _finish_job(
        self,
        job_id: int,
        *,
        success: int,
        failure: int,
        skipped: int,
        errors: list[str],
        stats: ClassificationRequestStats | None = None,
    ) -> None:
        if failure == 0:
            status = "completed"
        elif success > 0 or skipped > 0:
            status = "partial_failure"
        else:
            status = "failed"
        with self._database.session() as session:
            job = session.get(AIJob, job_id)
            if job is not None:
                job.status = status
                job.success_count = success
                job.failure_count = failure
                job.skipped_count = skipped
                job.processed_count = min(job.total_count, success + failure + skipped)
                job.error_summary = (
                    sanitize_error("\n".join(errors[:5]), limit=300) if errors else None
                )
                job.finished_at = datetime.now(UTC)
                if stats is not None:
                    job.model_request_count = stats.model_requests
                    job.retry_count = stats.retries
                    job.parse_failure_count = stats.parse_failures
                    job.split_count = stats.splits
                session.commit()
                if stats is not None:
                    elapsed_ms = _elapsed_ms(job.started_at, job.finished_at) or 0
                    logger.info(
                        "AI classification benchmark job=%s items=%s batches=%s requests=%s "
                        "retries=%s parse_failures=%s splits=%s elapsed_ms=%s average_ms=%s",
                        job.id,
                        job.total_count,
                        job.total_batches,
                        stats.model_requests,
                        stats.retries,
                        stats.parse_failures,
                        stats.splits,
                        elapsed_ms,
                        round(elapsed_ms / job.total_count) if job.total_count else 0,
                    )


def _build_provider(config: AIConfig) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout_seconds=config.timeout_seconds,
    )


def _chunks[T](items: list[T], size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _needs_smart_review(result: BatchClassificationResult, item: BatchClassificationItem) -> bool:
    """Use deterministic ambiguity signals in addition to model confidence."""

    if not item.summary:
        return False
    if result.confidence < AIOperationService.SMART_CONFIDENCE_THRESHOLD:
        return True
    if result.category is Category.UNCLASSIFIED:
        return True
    title = item.title.casefold().strip()
    groups = (
        ("模型", "算法", "推理", "训练", "框架", "开源"),
        ("agent", "智能体", "助手", "copilot", "工作流"),
        ("落地", "采用", "投产", "应用案例", "降本增效"),
        ("获奖", "入围", "名单", "公示", "表彰", "排行榜", "benchmark"),
        ("征集", "申报", "报名", "招募", "截止"),
        ("政策", "标准", "白皮书", "报告", "监管"),
    )
    boundary_count = sum(any(word in title for word in group) for group in groups)
    lacks_event = not any(
        word in title
        for word in ("发布", "上线", "推出", "开源", "采用", "落地", "公布", "征集", "印发")
    )
    ambiguous_ranking = any(word in title for word in ("benchmark", "排行榜", "leaderboard"))
    risk = 0
    risk += 1 if len(title) < 14 else 0
    risk += 3 if boundary_count > 1 else 0
    risk += 1 if lacks_event else 0
    risk += 2 if ambiguous_ranking and result.category is Category.AWARD_CASE else 0
    return risk >= 3


async def _call_batch_provider(
    provider: OpenAICompatibleProvider,
    batch: list[BatchClassificationItem],
    *,
    include_summaries: bool,
    output_mode: str,
) -> BatchParseResult:
    parameters = inspect.signature(provider.classify_batch).parameters
    if "output_mode" in parameters:
        return await provider.classify_batch(
            batch,
            include_summaries=include_summaries,
            output_mode=output_mode,
        )
    return await provider.classify_batch(batch, include_summaries=include_summaries)


def _classification_context(item: IntelligenceItem) -> str | None:
    parts: list[str] = []
    if item.summary:
        parts.append(item.summary.strip())
    for key in ("description", "lead", "content", "body"):
        value = item.extra.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    combined = "\n".join(parts).strip()
    return combined[:1600] or None


def _merge_parse_results(left: BatchParseResult, right: BatchParseResult) -> BatchParseResult:
    return BatchParseResult(
        results={**left.results, **right.results},
        failures={**left.failures, **right.failures},
        unknown_ids=left.unknown_ids + right.unknown_ids,
    )


def _request_signature(job_type: str, item_ids: list[int], mode: str, flag: bool) -> str:
    source = f"{job_type}|{mode}|{int(flag)}|" + ",".join(str(value) for value in item_ids)
    return hashlib.sha256(source.encode()).hexdigest()


def _summary_prompt(title: str, summary: str | None) -> str:
    parts = ["请用2-3句简洁中文总结以下 AI 资讯。禁止编造, 只使用数据库已有信息。"]
    parts.append(f"标题: {title}")
    parts.append(f"已有摘要或正文数据: {summary or '无'}")
    parts.append("只输出总结文本, 不要加前缀。")
    return "\n".join(parts)


def _elapsed_ms(started_at: datetime, finished_at: datetime | None) -> int | None:
    if finished_at is None:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    return max(0, round((finished_at - started_at).total_seconds() * 1000))
