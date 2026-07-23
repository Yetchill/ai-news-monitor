"""Persistence-neutral taxonomy-v2, verification and publication values."""

from dataclasses import dataclass
from datetime import datetime

from app.domain.enums import (
    CaseCompleteness,
    IndustryTag,
    PrimaryType,
    ReviewStatus,
    TopicTag,
    VerificationStatus,
)

TAXONOMY_VERSION = "v2"


@dataclass(frozen=True, slots=True)
class OpportunityFields:
    organizer: str | None = None
    application_name: str | None = None
    application_target: str | None = None
    deadline_at: datetime | None = None
    application_method: str | None = None
    application_url: str | None = None


@dataclass(frozen=True, slots=True)
class TaxonomyResult:
    primary_type: PrimaryType
    topic_tags: tuple[TopicTag, ...]
    industry_tags: tuple[IndustryTag, ...]
    case_completeness: CaseCompleteness
    opportunity: OpportunityFields
    taxonomy_version: str
    matched_rules: tuple[str, ...]
    reason: str
    score: float


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verification_status: VerificationStatus
    review_status: ReviewStatus
    discovery_url: str | None
    official_url: str | None
    origin_publisher: str | None
    matched_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    leadership_homepage: bool
    formal_export: bool
    industry_leads: bool
    review_queue: bool
    reasons: tuple[str, ...]
