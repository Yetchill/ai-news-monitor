"""Persistence-neutral query values used by application and Web adapters."""

from dataclasses import dataclass
from datetime import datetime

from app.domain.enums import (
    CaseCompleteness,
    Category,
    CrawlMode,
    CrawlStatus,
    ImplementationStatus,
    LifecycleState,
    PrimaryType,
    ReviewPolicy,
    ReviewStatus,
    RunTrigger,
    SourceKind,
    SourceRole,
    SourceScope,
    SourceType,
    VerificationStatus,
)


@dataclass(frozen=True, slots=True)
class Page[EntryT]:
    entries: tuple[EntryT, ...]
    page: int
    per_page: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)


@dataclass(frozen=True, slots=True)
class ItemFilter:
    """Filters shared by paginated lists and complete bounded exports."""

    keyword: str | None = None
    category: Category | None = None
    primary_type: PrimaryType | None = None
    verification_status: VerificationStatus | None = None
    review_status: ReviewStatus | None = None
    source_id: int | None = None
    favorite: bool | None = None
    published_from: datetime | None = None
    published_to: datetime | None = None
    discovered_from: datetime | None = None
    discovered_to: datetime | None = None
    unclassified: bool | None = None
    is_read: bool | None = None
    source_scope: SourceScope = SourceScope.LEADERSHIP


@dataclass(frozen=True, slots=True)
class ItemQuery(ItemFilter):
    page: int = 1
    per_page: int = 20


@dataclass(frozen=True, slots=True)
class ItemListEntry:
    id: int
    title: str
    original_url: str | None
    summary: str | None
    published_at: datetime | None
    discovered_at: datetime
    updated_at: datetime
    automatic_category: Category
    manual_category: Category | None
    is_favorite: bool
    source_id: int
    source_name: str
    source_kind: SourceKind
    primary_type: PrimaryType = PrimaryType.UNCLASSIFIED
    manual_primary_type: PrimaryType | None = None
    topic_tags: tuple[str, ...] = ()
    industry_tags: tuple[str, ...] = ()
    verification_status: VerificationStatus = VerificationStatus.MEDIA_ONLY
    review_status: ReviewStatus = ReviewStatus.PENDING
    case_completeness: CaseCompleteness = CaseCompleteness.NOT_CASE
    discovery_url: str | None = None
    official_url: str | None = None
    is_read: bool = False
    ai_summary: str | None = None
    ai_summary_model: str | None = None
    classification_score: float | None = None
    classification_reason: str | None = None
    automatic_category_provider: str | None = None

    @property
    def effective_category(self) -> Category:
        return self.manual_category or self.automatic_category

    @property
    def category_origin(self) -> str:
        return "人工" if self.manual_category is not None else "自动"

    @property
    def effective_primary_type(self) -> PrimaryType:
        return self.manual_primary_type or self.primary_type


@dataclass(frozen=True, slots=True)
class SourceOption:
    id: int
    name: str
    source_kind: SourceKind
    enabled: bool
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE


@dataclass(frozen=True, slots=True)
class SourceListEntry:
    id: int
    name: str
    start_url: str | None
    source_type: SourceType
    collector_name: str
    enabled: bool
    default_category: Category | None
    discovery_status: str | None
    discovery_confidence: float | None
    requires_custom_collector: bool
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    source_kind: SourceKind
    source_tier: str
    audience: str
    homepage_visible: bool
    export_visible: bool
    slug: str | None = None
    lifecycle_state: LifecycleState = LifecycleState.CANDIDATE
    source_role: SourceRole = SourceRole.FALLBACK
    crawl_mode: CrawlMode = CrawlMode.CUSTOM
    review_policy: ReviewPolicy = ReviewPolicy.ALWAYS_REVIEW
    implementation_status: ImplementationStatus = ImplementationStatus.RESEARCH_NEEDED
    implementation_reason: str | None = None
    last_preview_at: datetime | None = None
    preview_item_count: int | None = None
    allowed_primary_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemOverview:
    total: int
    unread: int
    added_today: int
    unclassified: int


@dataclass(frozen=True, slots=True)
class AIWorkOverview:
    unclassified: int
    unsummarized: int


@dataclass(frozen=True, slots=True)
class SourceOverview:
    total: int
    active: int
    attention: int
    candidate: int


@dataclass(frozen=True, slots=True)
class SourcePeriodStats:
    discovered: int
    accepted: int
    rejected: int
    new: int
    updated: int
    failed: int
    succeeded_runs: int
    failed_runs: int


@dataclass(frozen=True, slots=True)
class SourceRecentRun:
    run_id: int
    started_at: datetime
    trigger: RunTrigger
    run_status: CrawlStatus
    source_status: str
    discovered: int
    accepted: int
    rejected: int
    new: int
    updated: int
    duplicate: int
    failed: int
    error: str | None


@dataclass(frozen=True, slots=True)
class SourceRecentItem:
    id: int
    title: str
    original_url: str | None
    discovered_at: datetime
    category: Category


@dataclass(frozen=True, slots=True)
class CrawlSourceExecutionEntry:
    source_id: int
    source_name: str
    status: str
    discovered: int
    accepted: int
    rejected: int
    new: int
    updated: int
    duplicate: int
    failed: int
    error: str | None


@dataclass(frozen=True, slots=True)
class CrawlRunListEntry:
    id: int
    status: CrawlStatus
    trigger: RunTrigger
    started_at: datetime
    finished_at: datetime | None
    source_total: int
    source_success: int
    source_failed: int
    discovered_count: int
    new_count: int
    updated_count: int
    skipped_count: int
    unclassified_count: int
    error_summary: str | None
    normalized_count: int
    accepted_count: int
    rejected_count: int
    classified_count: int
    duplicate_count: int
    failed_count: int
    rejection_reason_counts: dict[str, int]
    failure_reason_counts: dict[str, int]
    source_executions: tuple[CrawlSourceExecutionEntry, ...] = ()

    @property
    def duration_seconds(self) -> int | None:
        if self.finished_at is None:
            return None
        return max(0, int((self.finished_at - self.started_at).total_seconds()))


@dataclass(frozen=True, slots=True)
class CrawlRunOverview:
    total: int
    success: int
    partial_success: int
    failed: int
