"""Preview recording and guarded candidate activation."""

from collections.abc import Callable, Collection
from datetime import UTC, datetime

from app.domain.enums import ImplementationStatus, LifecycleState
from app.domain.models import Source
from app.domain.update import SourcePreviewResult, SourceUpdateStatus
from app.storage.repositories import RepositoryUnitOfWork

UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]


class SourceActivationError(ValueError):
    pass


class SourceLifecycleService:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, supported_collectors: Collection[str]
    ) -> None:
        self._uow_factory = uow_factory
        self._supported_collectors = frozenset(supported_collectors)

    def get_by_slug(self, slug: str) -> Source:
        with self._uow_factory() as uow:
            source = uow.sources.get_by_slug(slug)
            if source is None:
                raise LookupError(f"source slug {slug!r} does not exist")
            return source

    def record_preview(self, slug: str, result: SourcePreviewResult) -> None:
        with self._uow_factory() as uow:
            source = uow.sources.get_by_slug(slug)
            if source is None:
                raise LookupError(f"source slug {slug!r} does not exist")
            source.last_preview_at = datetime.now(UTC)
            source.preview_item_count = result.normalized
            source.preview_result = _preview_payload(result)

    def activate(self, slug: str, result: SourcePreviewResult, *, confirm: bool) -> Source:
        if not confirm:
            raise SourceActivationError("activation requires explicit --confirm")
        with self._uow_factory() as uow:
            source = uow.sources.get_by_slug(slug)
            if source is None:
                raise LookupError(f"source slug {slug!r} does not exist")
            source.last_preview_at = datetime.now(UTC)
            source.preview_item_count = result.normalized
            source.preview_result = _preview_payload(result)
            _require_activatable(source, result, self._supported_collectors)
            source.lifecycle_state = LifecycleState.ACTIVE
            source.enabled = True
            source.implementation_status = ImplementationStatus.READY
            source.implementation_reason = "无落库 preview 通过激活门槛。"
            source.activation_evidence = (
                f"preview fetched={result.fetched} normalized={result.normalized} "
                f"valid_title={result.valid_title_ratio:.2f} "
                f"valid_link={result.valid_link_ratio:.2f}"
            )
            source.verified_at = source.last_preview_at
            return source


def _require_activatable(
    source: Source,
    result: SourcePreviewResult,
    supported_collectors: frozenset[str],
) -> None:
    if source.lifecycle_state is not LifecycleState.CANDIDATE:
        raise SourceActivationError("only candidate sources can be activated")
    if source.collector_name not in supported_collectors:
        raise SourceActivationError(f"collector {source.collector_name!r} is not supported")
    if source.implementation_status in {
        ImplementationStatus.BLOCKED_BY_JAVASCRIPT,
        ImplementationStatus.NEEDS_CUSTOM_COLLECTOR,
    }:
        raise SourceActivationError(source.implementation_reason or "source is technically blocked")
    if result.status is not SourceUpdateStatus.SUCCESS:
        raise SourceActivationError(result.error or "preview fetch/parse failed")
    if result.fetched < 1 or result.normalized < 1:
        raise SourceActivationError("preview must extract at least one valid item")
    if result.valid_title_ratio < 0.8 or result.valid_link_ratio < 0.8:
        raise SourceActivationError(
            "preview title/link validity is below the 80% activation threshold"
        )
    date_fallback = source.collector_config.get("date_fallback")
    if result.valid_date_ratio < 0.7 and not (
        isinstance(date_fallback, str) and date_fallback.strip()
    ):
        raise SourceActivationError("preview date validity is below the 70% activation threshold")
    if result.duplicate_ratio > 0.2:
        raise SourceActivationError("preview duplicate ratio exceeds the 20% activation threshold")
    if result.failed:
        raise SourceActivationError("preview contains processing failures")


def _preview_payload(result: SourcePreviewResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "fetch_status": result.fetch_status,
        "parse_status": result.parse_status,
        "fetched": result.fetched,
        "normalized": result.normalized,
        "accepted": result.accepted,
        "rejected": result.rejected,
        "failed": result.failed,
        "primary_type_counts": dict(result.primary_type_counts),
        "verification_status_counts": dict(result.verification_status_counts),
        "review_status_counts": dict(result.review_status_counts),
        "valid_title_ratio": result.valid_title_ratio,
        "valid_date_ratio": result.valid_date_ratio,
        "valid_link_ratio": result.valid_link_ratio,
        "external_link_ratio": result.external_link_ratio,
        "duplicate_count": result.duplicate_count,
        "duplicate_ratio": result.duplicate_ratio,
        "rejection_reason_counts": dict(result.rejection_reason_counts),
        "failure_reason_counts": dict(result.failure_reason_counts),
    }
