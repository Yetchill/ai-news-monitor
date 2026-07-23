"""Shared update pipeline used by CLI and future UI or scheduling adapters."""

import asyncio
import logging
import traceback
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import cast
from urllib.parse import urlsplit

from app.domain.classification import ClassificationResult
from app.domain.collection import CollectedItem
from app.domain.enums import Category, LifecycleState, RunTrigger
from app.domain.models import Source
from app.domain.update import (
    SourcePreviewItem,
    SourcePreviewResult,
    SourceUpdateResult,
    SourceUpdateStatus,
    UpdateMode,
    UpdateResult,
)
from app.fetchers.errors import FetchError
from app.services.classification_service import ClassificationService
from app.services.content_admission import BasicAdmissionPolicy, ContentAdmissionPolicy
from app.services.crawl_run_service import CrawlRunService
from app.services.crawl_service import CrawlService
from app.services.error_sanitization import sanitize_error
from app.services.item_normalization import ItemNormalizationError, normalize_collected_item
from app.services.item_persistence_service import ClassifiedItem, ItemPersistenceService
from app.services.publication_policy import PublicationPolicy
from app.services.verification_service import VerificationService
from app.storage.repositories import RepositoryUnitOfWork

LOGGER = logging.getLogger("app.crawler.pipeline")
UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]


class SourceNotFoundError(LookupError):
    """Raised when a requested source id does not exist."""


class SourceDisabledError(ValueError):
    """Raised when a disabled source is explicitly requested without permission."""


class SourceCandidateError(ValueError):
    """Raised when a candidate tries to bypass preview/activation via update."""


class UpdatePipeline:
    """Coordinate source selection, collection, normalization, classification, and persistence."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        crawl_service: CrawlService,
        classification_service: ClassificationService,
        persistence_service: ItemPersistenceService | None = None,
        crawl_run_service: CrawlRunService | None = None,
        admission_policy: ContentAdmissionPolicy | None = None,
        verification_service: VerificationService | None = None,
        publication_policy: PublicationPolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._crawl_service = crawl_service
        self._classification_service = classification_service
        self._persistence_service = persistence_service or ItemPersistenceService(uow_factory)
        self._crawl_run_service = crawl_run_service or CrawlRunService(uow_factory)
        self._admission_policy = admission_policy or BasicAdmissionPolicy()
        self._verification_service = verification_service or VerificationService()
        self._publication_policy = publication_policy or PublicationPolicy()

    async def update(
        self,
        *,
        source_id: int | None = None,
        allow_disabled: bool = False,
        formal_only: bool = False,
        mode: UpdateMode = UpdateMode.INCREMENTAL,
        max_pages: int | None = None,
        max_items: int | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        trigger: RunTrigger = RunTrigger.MANUAL_CLI,
    ) -> UpdateResult:
        sources = self._select_sources(
            source_id=source_id,
            allow_disabled=allow_disabled,
            formal_only=formal_only,
        )
        crawl_run_id = self._crawl_run_service.start(source_total=len(sources), trigger=trigger)
        source_results: list[SourceUpdateResult] = []
        try:
            for source in sources:
                result = await self._update_source(
                    source,
                    crawl_run_id=crawl_run_id,
                    mode=mode,
                    max_pages=max_pages,
                    max_items=max_items,
                    published_from=published_from,
                    published_to=published_to,
                )
                source_results.append(result)
            return self._crawl_run_service.finish(crawl_run_id, source_results)
        except asyncio.CancelledError as exc:
            _log_exception("Update pipeline cancelled", exc)
            try:
                self._crawl_run_service.finish(
                    crawl_run_id,
                    source_results,
                    fatal_error="update cancelled during application shutdown",
                )
            except Exception as finish_error:
                _log_exception(
                    "Failed to move cancelled CrawlRun out of running state", finish_error
                )
            raise
        except Exception as exc:
            _log_exception("Uncaught update pipeline failure", exc)
            try:
                self._crawl_run_service.finish(
                    crawl_run_id,
                    source_results,
                    fatal_error=exc,
                )
            except Exception as finish_error:
                _log_exception("Failed to move CrawlRun out of running state", finish_error)
            raise

    def _select_sources(
        self, *, source_id: int | None, allow_disabled: bool, formal_only: bool = False
    ) -> list[Source]:
        with self._uow_factory() as uow:
            if source_id is None:
                if allow_disabled:
                    return [
                        source
                        for source in uow.sources.list()
                        if source.lifecycle_state is not LifecycleState.CANDIDATE
                    ]
                if formal_only:
                    return [
                        source
                        for source in uow.sources.list_enabled_formal()
                        if source.lifecycle_state is LifecycleState.ACTIVE
                    ]
                return uow.sources.list_active()
            source = uow.sources.get(source_id)
            if source is None:
                raise SourceNotFoundError(f"source {source_id} does not exist")
            if source.lifecycle_state is LifecycleState.CANDIDATE:
                raise SourceCandidateError(
                    f"source {source_id} is candidate; preview and activate it before updating"
                )
            if not source.enabled and not allow_disabled:
                raise SourceDisabledError(
                    f"source {source_id} is disabled; pass allow_disabled=True to update it"
                )
            return [source]

    async def preview(self, source_id: int, *, max_items: int = 10) -> SourcePreviewResult:
        """Run collection, admission, taxonomy and verification without persistence."""

        with self._uow_factory() as uow:
            source = uow.sources.get(source_id)
            if source is None:
                raise SourceNotFoundError(f"source {source_id} does not exist")
        failures: Counter[str] = Counter()
        rejections: Counter[str] = Counter()
        try:
            self._admission_policy.validate_source(source)
        except ValueError as exc:
            failures["source.configuration_invalid"] += 1
            return _preview_failure(source, failures, exc)

        try:
            collected = await self._crawl_service.collect(source, max_items=max_items)
        except Exception as exc:
            failures[_failure_reason(exc, "collection")] += 1
            return _preview_failure(source, failures, exc)

        items: list[SourcePreviewItem] = []
        normalized_count = 0
        keep_query_params = _keep_query_params(source)
        primary_counts: Counter[str] = Counter()
        verification_counts: Counter[str] = Counter()
        review_counts: Counter[str] = Counter()
        valid_dates = 0
        valid_links = 0
        external_links = 0
        duplicate_count = 0
        seen_urls: set[str] = set()
        source_host = (urlsplit(source.start_url).hostname or "").casefold()
        for collected_item in collected:
            try:
                item = normalize_collected_item(collected_item, keep_query_params=keep_query_params)
            except ItemNormalizationError:
                failures["normalization.failed"] += 1
                continue
            normalized_count += 1
            if item.canonical_url in seen_urls:
                duplicate_count += 1
            else:
                seen_urls.add(item.canonical_url)
            decision = self._admission_policy.admit(item, source)
            if not decision.accepted:
                rejections[decision.reason] += 1
            taxonomy = self._classification_service.classify_v2(
                item, source_role=source.source_role
            )
            verification = self._verification_service.verify(item, source)
            self._publication_policy.decide(
                source=source,
                admission_accepted=decision.accepted,
                taxonomy=taxonomy,
                verification=verification,
            )
            primary_counts[taxonomy.primary_type.value] += 1
            verification_counts[verification.verification_status.value] += 1
            review_counts[verification.review_status.value] += 1
            valid_dates += item.published_at is not None
            host = (urlsplit(item.original_url).hostname or "").casefold()
            valid_links += bool(host)
            external_links += bool(host and host != source_host)
            items.append(
                SourcePreviewItem(
                    title=item.title,
                    original_url=item.original_url,
                    accepted=decision.accepted,
                    reason=decision.reason,
                    quality_score=decision.quality_score,
                    published_at=item.published_at,
                    primary_type=taxonomy.primary_type,
                    verification_status=verification.verification_status,
                    review_status=verification.review_status,
                    link_domain=host or None,
                    classification_reason=taxonomy.reason,
                )
            )
        denominator = normalized_count or 1
        return SourcePreviewResult(
            source_id=source.id,
            source_name=source.name,
            status=(
                SourceUpdateStatus.FAILED
                if collected and normalized_count == 0 and failures
                else SourceUpdateStatus.SUCCESS
            ),
            fetched=len(collected),
            normalized=normalized_count,
            accepted=sum(item.accepted for item in items),
            rejected=sum(not item.accepted for item in items),
            failed=sum(failures.values()),
            rejection_reason_counts=dict(rejections),
            failure_reason_counts=dict(failures),
            items=tuple(items),
            primary_type_counts=dict(primary_counts),
            verification_status_counts=dict(verification_counts),
            review_status_counts=dict(review_counts),
            valid_title_ratio=normalized_count / denominator,
            valid_date_ratio=valid_dates / denominator,
            valid_link_ratio=valid_links / denominator,
            external_link_ratio=external_links / denominator,
            duplicate_count=duplicate_count,
            duplicate_ratio=duplicate_count / denominator,
        )

    async def _update_source(
        self,
        source: Source,
        *,
        crawl_run_id: int,
        mode: UpdateMode,
        max_pages: int | None,
        max_items: int | None,
        published_from: datetime | None,
        published_to: datetime | None,
    ) -> SourceUpdateResult:
        discovered = 0
        normalized_count = 0
        rejected_count = 0
        failed_count = 0
        rejection_reasons: Counter[str] = Counter()
        failure_reasons: Counter[str] = Counter()
        phase = "configuration"
        try:
            self._admission_policy.validate_source(source)
            phase = "collection"
            collected = await self._crawl_service.collect(
                source,
                mode=mode,
                max_pages=max_pages,
                max_items=max_items or source.max_items_per_run,
                published_from=(
                    published_from
                    or (
                        datetime.now(UTC) - timedelta(days=source.lookback_days)
                        if mode is UpdateMode.INCREMENTAL and source.lookback_days > 0
                        else None
                    )
                ),
                published_to=published_to,
            )
            discovered = len(collected)
            phase = "normalization"
            normalized_items: list[CollectedItem] = []
            keep_query_params = _keep_query_params(source)
            for item in collected:
                try:
                    normalized_items.append(
                        normalize_collected_item(item, keep_query_params=keep_query_params)
                    )
                except ItemNormalizationError as exc:
                    failed_count += 1
                    failure_reasons["normalization.failed"] += 1
                    LOGGER.warning(
                        "Skipped invalid item from source_id=%s: %s",
                        source.id,
                        sanitize_error(exc),
                    )

            if collected and not normalized_items:
                error = "all collected records failed normalization"
                self._mark_source_failed(source.id, error)
                return SourceUpdateResult(
                    source_id=source.id,
                    source_name=source.name,
                    status=SourceUpdateStatus.FAILED,
                    discovered=discovered,
                    skipped=failed_count,
                    failed=failed_count,
                    failure_reason_counts=dict(failure_reasons),
                    error=error,
                )

            normalized_count = len(normalized_items)
            phase = "admission"
            admitted_items: list[CollectedItem] = []
            for item in normalized_items:
                decision = self._admission_policy.admit(item, source)
                if decision.accepted:
                    admitted_items.append(item)
                    LOGGER.info(
                        "Content accepted source_id=%s stage=admission item_ref=%s "
                        "reason=%s score=%s rules=%s",
                        source.id,
                        _item_ref(item),
                        decision.reason,
                        decision.quality_score,
                        ",".join(match.rule_id for match in decision.matched_rules),
                    )
                    continue
                rejected_count += 1
                rejection_reasons[decision.reason] += 1
                if decision.reason == "source.configuration_invalid":
                    LOGGER.warning(
                        "Admission configuration rejected source_id=%s stage=admission rules=%s",
                        source.id,
                        ",".join(match.rule_id for match in decision.matched_rules),
                    )
                LOGGER.info(
                    "Content rejected source_id=%s stage=admission item_ref=%s "
                    "reason=%s score=%s rules=%s",
                    source.id,
                    _item_ref(item),
                    decision.reason,
                    decision.quality_score,
                    ",".join(match.rule_id for match in decision.matched_rules),
                )

            phase = "classification"
            classified_items: list[ClassifiedItem] = []
            classified_count = 0
            for item in admitted_items:
                try:
                    classification = await self._classification_service.classify(
                        item,
                        source_default=source.default_category,
                    )
                    taxonomy = self._classification_service.classify_v2(
                        item, source_role=source.source_role
                    )
                    verification = self._verification_service.verify(item, source)
                    self._publication_policy.decide(
                        source=source,
                        admission_accepted=True,
                        taxonomy=taxonomy,
                        verification=verification,
                    )
                    classified_count += 1
                except Exception as exc:
                    failed_count += 1
                    failure_reasons["classification.failed"] += 1
                    _log_exception(
                        f"Classification failed for source_id={source.id}; using unclassified",
                        exc,
                    )
                    classification = ClassificationResult(
                        category=Category.UNCLASSIFIED,
                        score=0.0,
                        reason=f"classification failed: {sanitize_error(exc)}",
                        provider="classification_error",
                    )
                    taxonomy = self._classification_service.classify_v2(
                        item, source_role=source.source_role
                    )
                    verification = self._verification_service.verify(item, source)
                classified_items.append(
                    ClassifiedItem(item, classification, taxonomy, verification)
                )

            phase = "persistence"
            stats = self._persistence_service.persist_source(
                source_id=source.id,
                source_name=source.name,
                crawl_run_id=crawl_run_id,
                items=classified_items,
                invalid_skipped=failure_reasons["normalization.failed"],
                checked_at=datetime.now(UTC),
            )
            return SourceUpdateResult(
                source_id=source.id,
                source_name=source.name,
                status=SourceUpdateStatus.SUCCESS,
                discovered=discovered,
                new=stats.new,
                updated=stats.updated,
                skipped=stats.skipped,
                unclassified=stats.unclassified,
                normalized=normalized_count,
                accepted=len(admitted_items),
                rejected=rejected_count,
                classified=classified_count,
                duplicate=stats.duplicate,
                failed=failed_count,
                rejection_reason_counts=dict(rejection_reasons),
                failure_reason_counts=dict(failure_reasons),
            )
        except Exception as exc:
            safe_error = sanitize_error(exc)
            failure_reasons[_failure_reason(exc, phase)] += 1
            _log_exception(f"Source update failed for source_id={source.id}", exc)
            self._mark_source_failed(source.id, safe_error)
            return SourceUpdateResult(
                source_id=source.id,
                source_name=source.name,
                status=SourceUpdateStatus.FAILED,
                discovered=discovered,
                normalized=normalized_count,
                rejected=rejected_count,
                skipped=failure_reasons["normalization.failed"],
                failed=failed_count + 1,
                rejection_reason_counts=dict(rejection_reasons),
                failure_reason_counts=dict(failure_reasons),
                error=safe_error,
            )

    def _mark_source_failed(self, source_id: int, error: str) -> None:
        try:
            with self._uow_factory() as uow:
                source = uow.sources.get(source_id)
                if source is None:
                    raise LookupError(f"source {source_id} no longer exists")
                source.last_checked_at = datetime.now(UTC)
                source.last_error = sanitize_error(error)
        except Exception as state_error:
            _log_exception(
                f"Failed to persist failure state for source_id={source_id}",
                state_error,
            )


def _log_exception(context: str, error: BaseException) -> None:
    stack = " | ".join(
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    )
    LOGGER.error("%s: %s; traceback=%s", context, sanitize_error(error), stack or "unavailable")


def _keep_query_params(source: Source) -> tuple[str, ...] | None:
    value = source.collector_config.get("keep_query_params")
    if not isinstance(value, list):
        return None
    values = cast(list[object], value)
    return tuple(item for item in values if isinstance(item, str))


def _item_ref(item: CollectedItem) -> str:
    """Return a stable short audit reference without logging title, body, or URL."""

    return sha256(item.canonical_url.encode("utf-8")).hexdigest()[:16]


def _failure_reason(error: BaseException, phase: str) -> str:
    if phase == "configuration" or (
        phase == "collection" and isinstance(error, (ValueError, LookupError))
    ):
        return "source.configuration_invalid"
    if isinstance(error, FetchError):
        return "fetch.failed"
    if phase == "collection":
        return "parse_or_collection.failed"
    if phase == "persistence":
        return "persistence.failed"
    return f"{phase}.failed"


def _preview_failure(
    source: Source, failures: Counter[str], error: BaseException
) -> SourcePreviewResult:
    fetch_failed = isinstance(error, FetchError)
    return SourcePreviewResult(
        source_id=source.id,
        source_name=source.name,
        status=SourceUpdateStatus.FAILED,
        failed=sum(failures.values()),
        failure_reason_counts=dict(failures),
        error=sanitize_error(error),
        fetch_status="failed" if fetch_failed else "success",
        parse_status="not_run" if fetch_failed else "failed",
    )
