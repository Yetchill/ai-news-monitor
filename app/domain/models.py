"""SQLAlchemy 2.x mappings for the core persistence model."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceRole,
    SourceTier,
    SourceType,
    VerificationStatus,
)


def utc_now() -> datetime:
    """Return the current UTC time for model defaults."""

    return datetime.now(UTC)


def enum_values[EnumT: StrEnum](members: type[EnumT]) -> list[str]:
    """Return stable persisted values for a string enum."""

    return [member.value for member in members]


def enum_type[EnumT: StrEnum](enum_class: type[EnumT]) -> Enum:
    """Create a portable string-backed SQLAlchemy enum."""

    return Enum(
        enum_class,
        values_callable=enum_values,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


class Base(DeclarativeBase):
    """Declarative mapping base."""


class Source(Base):
    """A configured entry point from which intelligence will eventually be collected."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[SourceType] = mapped_column(enum_type(SourceType), nullable=False)
    start_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    lifecycle_state: Mapped[LifecycleState] = mapped_column(
        enum_type(LifecycleState), nullable=False, default=LifecycleState.ACTIVE
    )
    source_role: Mapped[SourceRole] = mapped_column(
        enum_type(SourceRole), nullable=False, default=SourceRole.FALLBACK
    )
    crawl_mode: Mapped[CrawlMode] = mapped_column(
        enum_type(CrawlMode), nullable=False, default=CrawlMode.CUSTOM
    )
    review_policy: Mapped[ReviewPolicy] = mapped_column(
        enum_type(ReviewPolicy), nullable=False, default=ReviewPolicy.ALWAYS_REVIEW
    )
    allowed_primary_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_items_per_run: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    implementation_status: Mapped[ImplementationStatus] = mapped_column(
        enum_type(ImplementationStatus),
        nullable=False,
        default=ImplementationStatus.RESEARCH_NEEDED,
    )
    implementation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activation_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_preview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preview_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    preview_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    catalog_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    catalog_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_category: Mapped[Category | None] = mapped_column(enum_type(Category), nullable=True)
    collector_name: Mapped[str] = mapped_column(String(100), nullable=False)
    collector_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    discovery_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    discovery_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    requires_custom_collector: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin: Mapped[SourceOrigin] = mapped_column(
        enum_type(SourceOrigin), nullable=False, default=SourceOrigin.PRESET
    )
    source_kind: Mapped[SourceKind] = mapped_column(
        enum_type(SourceKind), nullable=False, default=SourceKind.TEST
    )
    source_tier: Mapped[SourceTier] = mapped_column(
        enum_type(SourceTier), nullable=False, default=SourceTier.FALLBACK
    )
    audience: Mapped[SourceAudience] = mapped_column(
        enum_type(SourceAudience), nullable=False, default=SourceAudience.ALL
    )
    homepage_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    export_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_scope: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    include_terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    exclude_terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    minimum_quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    accept_title_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_external_links: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_technical_updates: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    items: Mapped[list["IntelligenceItem"]] = relationship(
        back_populates="source", passive_deletes=True
    )
    crawl_executions: Mapped[list["CrawlSourceExecution"]] = relationship(
        back_populates="source", passive_deletes=True
    )


class IntelligenceItem(Base):
    """A normalized intelligence record discovered from a source."""

    __tablename__ = "intelligence_items"
    __table_args__ = (
        UniqueConstraint("source_id", "fingerprint", name="uq_item_source_fingerprint"),
        Index("ix_items_category_discovered", "category", "discovered_at"),
        Index("ix_items_primary_type_discovered", "primary_type", "discovered_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False, unique=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    category: Mapped[Category] = mapped_column(
        enum_type(Category), nullable=False, default=Category.UNCLASSIFIED
    )
    classification_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    automatic_category_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    manual_category: Mapped[Category | None] = mapped_column(enum_type(Category), nullable=True)
    primary_type: Mapped[PrimaryType] = mapped_column(
        enum_type(PrimaryType), nullable=False, default=PrimaryType.UNCLASSIFIED
    )
    manual_primary_type: Mapped[PrimaryType | None] = mapped_column(
        enum_type(PrimaryType), nullable=True
    )
    topic_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    manual_topic_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    industry_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    manual_industry_tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_type(VerificationStatus),
        nullable=False,
        default=VerificationStatus.MEDIA_ONLY,
    )
    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus), nullable=False, default=ReviewStatus.PENDING
    )
    verification_manually_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_manually_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    case_completeness: Mapped[CaseCompleteness] = mapped_column(
        enum_type(CaseCompleteness), nullable=False, default=CaseCompleteness.NOT_CASE
    )
    # Directly-created legacy/test records remain identifiable.  The update pipeline and
    # migration always write v2 explicitly after deterministic classification.
    taxonomy_version: Mapped[str] = mapped_column(String(20), nullable=False, default="legacy")
    taxonomy_matched_rules: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    organizer: Mapped[str | None] = mapped_column(String(500), nullable=True)
    application_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    application_target: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    application_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    discovery_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    official_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    origin_publisher: Mapped[str | None] = mapped_column(String(500), nullable=True)
    parent_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("intelligence_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admission_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_summary_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    source: Mapped[Source] = relationship(back_populates="items")
    revisions: Mapped[list["ItemRevision"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )
    parent: Mapped["IntelligenceItem | None"] = relationship(
        remote_side="IntelligenceItem.id", back_populates="children"
    )
    children: Mapped[list["IntelligenceItem"]] = relationship(back_populates="parent")
    review_events: Mapped[list["ItemReviewEvent"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )


class CrawlRun(Base):
    """Summary and lifecycle state for one complete update run."""

    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[CrawlStatus] = mapped_column(
        enum_type(CrawlStatus), nullable=False, default=CrawlStatus.RUNNING
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        enum_type(RunTrigger), nullable=False, default=RunTrigger.LEGACY_MANUAL
    )
    source_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_success: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unclassified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    normalized_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    classified_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejection_reason_counts: Mapped[dict[str, int]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    failure_reason_counts: Mapped[dict[str, int]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    revisions: Mapped[list["ItemRevision"]] = relationship(back_populates="crawl_run")
    source_executions: Mapped[list["CrawlSourceExecution"]] = relationship(
        back_populates="crawl_run", cascade="all, delete-orphan", passive_deletes=True
    )


class CrawlSourceExecution(Base):
    """Per-source child record retained independently from a mixed CrawlRun."""

    __tablename__ = "crawl_source_executions"
    __table_args__ = (
        UniqueConstraint("crawl_run_id", "source_id", name="uq_run_source_execution"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    crawl_run_id: Mapped[int] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    crawl_run: Mapped[CrawlRun] = relationship(back_populates="source_executions")
    source: Mapped[Source] = relationship(back_populates="crawl_executions")


class ScheduleSettings(Base):
    """Singleton, strongly typed local scheduling configuration."""

    __tablename__ = "schedule_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_schedule_settings_singleton"),
        CheckConstraint("schedule_hour BETWEEN 0 AND 23", name="ck_schedule_hour"),
        CheckConstraint("schedule_minute BETWEEN 0 AND 59", name="ck_schedule_minute"),
        CheckConstraint("schedule_days_mask BETWEEN 1 AND 127", name="ck_schedule_days"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schedule_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    schedule_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schedule_days_mask: Mapped[int] = mapped_column(Integer, nullable=False, default=127)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    last_scheduled_trigger_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ItemRevision(Base):
    """A before/after snapshot for a material change to an intelligence item."""

    __tablename__ = "item_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("intelligence_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    crawl_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("crawl_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    old_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    new_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    item: Mapped[IntelligenceItem] = relationship(back_populates="revisions")
    crawl_run: Mapped[CrawlRun | None] = relationship(back_populates="revisions")


class ItemReviewEvent(Base):
    """Append-only audit record for manual taxonomy or review overrides."""

    __tablename__ = "item_review_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("intelligence_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    actor_source: Mapped[str] = mapped_column(String(100), nullable=False)
    old_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    new_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    item: Mapped[IntelligenceItem] = relationship(back_populates="review_events")


class AISettings(Base):
    """Singleton AI model configuration persisted in the local database."""

    __tablename__ = "ai_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_ai_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="deepseek")
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="https://api.deepseek.com")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="deepseek-chat")
    api_key: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    classifier_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="off")
    classifier_strategy: Mapped[str] = mapped_column(String(20), nullable=False, default="hybrid")
    summarizer_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="off")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class AIJob(Base):
    """An AI classification or summarization run."""

    __tablename__ = "ai_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
