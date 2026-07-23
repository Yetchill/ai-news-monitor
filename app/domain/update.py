"""Persistence-independent values returned by the update application services."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.enums import (
    CrawlStatus,
    PrimaryType,
    ReviewStatus,
    RunTrigger,
    VerificationStatus,
)


def _empty_reason_counts() -> dict[str, int]:
    return {}


class UpdateMode(StrEnum):
    """Select ordinary incremental collection or bounded history collection."""

    INCREMENTAL = "incremental"
    HISTORY = "history"


class SourceUpdateStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SourceUpdateResult:
    source_id: int
    source_name: str
    status: SourceUpdateStatus
    discovered: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    unclassified: int = 0
    error: str | None = None
    normalized: int = 0
    accepted: int = 0
    rejected: int = 0
    classified: int = 0
    duplicate: int = 0
    failed: int = 0
    rejection_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    failure_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)

    @property
    def primary_rejection_reason(self) -> str | None:
        return _primary_reason(self.rejection_reason_counts)

    @property
    def primary_failure_reason(self) -> str | None:
        return _primary_reason(self.failure_reason_counts)


@dataclass(frozen=True, slots=True)
class UpdateResult:
    crawl_run_id: int
    status: CrawlStatus
    started_at: datetime
    finished_at: datetime
    source_total: int
    source_success: int
    source_failed: int
    discovered_count: int
    new_count: int
    updated_count: int
    skipped_count: int
    unclassified_count: int
    error_summary: str | None
    source_results: tuple[SourceUpdateResult, ...]
    trigger: RunTrigger = RunTrigger.LEGACY_MANUAL
    normalized_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    classified_count: int = 0
    duplicate_count: int = 0
    failed_count: int = 0
    rejection_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    failure_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)


@dataclass(frozen=True, slots=True)
class SourcePreviewItem:
    title: str
    original_url: str
    accepted: bool
    reason: str
    quality_score: int
    published_at: datetime | None = None
    primary_type: PrimaryType = PrimaryType.UNCLASSIFIED
    verification_status: VerificationStatus = VerificationStatus.MEDIA_ONLY
    review_status: ReviewStatus = ReviewStatus.PENDING
    link_domain: str | None = None
    classification_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourcePreviewResult:
    source_id: int
    source_name: str
    status: SourceUpdateStatus
    fetched: int = 0
    normalized: int = 0
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    rejection_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    failure_reason_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    items: tuple[SourcePreviewItem, ...] = ()
    error: str | None = None
    fetch_status: str = "success"
    parse_status: str = "success"
    primary_type_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    verification_status_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    review_status_counts: dict[str, int] = field(default_factory=_empty_reason_counts)
    valid_title_ratio: float = 0.0
    valid_date_ratio: float = 0.0
    valid_link_ratio: float = 0.0
    external_link_ratio: float = 0.0
    duplicate_count: int = 0
    duplicate_ratio: float = 0.0

    @property
    def primary_rejection_reason(self) -> str | None:
        return _primary_reason(self.rejection_reason_counts)

    @property
    def primary_failure_reason(self) -> str | None:
        return _primary_reason(self.failure_reason_counts)


def _primary_reason(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    return min(counts, key=lambda reason: (-counts[reason], reason))
