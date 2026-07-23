"""Persistence-neutral values for bounded document exports."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.enums import (
    Category,
    PrimaryType,
    ReviewStatus,
    SourceKind,
    SourceRole,
    SourceScope,
    VerificationStatus,
)
from app.domain.queries import ItemFilter


class ExportFormat(StrEnum):
    EXCEL = "excel"
    WORD = "word"


@dataclass(frozen=True, slots=True)
class ExportQuery:
    filters: ItemFilter = field(
        default_factory=lambda: ItemFilter(source_scope=SourceScope.FORMAL_EXPORT)
    )
    limit: int | None = None


@dataclass(frozen=True, slots=True)
class ExportItem:
    id: int
    title: str
    summary: str | None
    original_url: str | None
    published_at: datetime | None
    discovered_at: datetime
    automatic_category: Category
    manual_category: Category | None
    source_id: int
    source_name: str
    source_kind: SourceKind
    is_favorite: bool
    classification_score: float | None
    classification_reason: str | None
    primary_type: PrimaryType = PrimaryType.UNCLASSIFIED
    manual_primary_type: PrimaryType | None = None
    topic_tags: tuple[str, ...] = ()
    industry_tags: tuple[str, ...] = ()
    verification_status: VerificationStatus = VerificationStatus.MEDIA_ONLY
    review_status: ReviewStatus = ReviewStatus.PENDING
    source_role: SourceRole = SourceRole.FALLBACK
    discovery_url: str | None = None
    official_url: str | None = None

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
class ExportMetadata:
    generated_at: datetime
    filter_summary: str
    item_count: int


@dataclass(frozen=True, slots=True)
class RenderedExport:
    content: bytes
    filename: str
    ascii_filename: str
    media_type: str


@dataclass(frozen=True, slots=True)
class ExportResult(RenderedExport):
    item_count: int


class ExportError(RuntimeError):
    """Base class for concise, user-facing export errors."""


class EmptyExportError(ExportError):
    """Raised instead of creating an empty document."""


class ExportLimitExceededError(ExportError):
    """Raised when a query would exceed the selected format's hard bound."""


class InvalidExportLimitError(ExportError):
    """Raised when a caller requests an invalid custom limit."""


class ExportGenerationError(RuntimeError):
    """Raised when a renderer cannot produce a valid office document."""
