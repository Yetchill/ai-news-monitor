"""Enumerated values persisted by the stage-one data model."""

from enum import StrEnum


class SourceType(StrEnum):
    RSS = "rss"
    HTML_LIST = "html_list"
    GITHUB_RELEASE = "github_release"
    JSON_API = "json_api"
    CUSTOM = "custom"


class Category(StrEnum):
    MODEL_TECHNOLOGY = "model_technology"
    AGENT_PRODUCT = "agent_product"
    ENTERPRISE_CASE = "enterprise_case"
    AWARD_CASE = "award_case"
    SOLICITATION = "solicitation"
    POLICY_INDUSTRY = "policy_industry"
    UNCLASSIFIED = "unclassified"


class SourceOrigin(StrEnum):
    PRESET = "preset"
    USER_ADDED = "user_added"
    IMPORTED = "imported"


class SourceKind(StrEnum):
    """Business standing of a configured source."""

    FORMAL = "formal"
    TEST = "test"
    FALLBACK = "fallback"


class SourceTier(StrEnum):
    """Editorial authority tier used by the admission policy."""

    GOVERNMENT = "government"
    OFFICIAL_COMPANY = "official_company"
    ASSOCIATION = "association"
    AUTHORITATIVE_MEDIA = "authoritative_media"
    FALLBACK = "fallback"


class SourceAudience(StrEnum):
    LEADERSHIP = "leadership"
    ALL = "all"


class SourceScope(StrEnum):
    """Stable item-list visibility scopes exposed by Web and export adapters."""

    LEADERSHIP = "leadership"
    FORMAL_EXPORT = "formal_export"
    ALL = "all"
    NON_FORMAL = "non_formal"
    DISABLED = "disabled"
    FALLBACK = "fallback"
    INDUSTRY_LEADS = "industry_leads"


class PrimaryType(StrEnum):
    """Taxonomy-v2 primary information form; exactly one is stored per item."""

    UNCLASSIFIED = "unclassified"
    PRODUCT_UPDATE = "product_update"
    POLICY_STANDARD = "policy_standard"
    APPLICATION_OPPORTUNITY = "application_opportunity"
    AWARD_RESULT = "award_result"
    REPORT_RELEASE = "report_release"
    CASE_ANALYSIS = "case_analysis"
    INDUSTRY_SIGNAL = "industry_signal"


class TopicTag(StrEnum):
    MODEL = "model"
    AGENT = "agent"
    AGENT_PLATFORM = "agent_platform"
    API = "api"
    OPEN_SOURCE = "open_source"
    INDUSTRY_APPLICATION = "industry_application"
    POLICY = "policy"
    STANDARD = "standard"
    AWARD = "award"
    CASE = "case"
    SAFETY_GOVERNANCE = "safety_governance"
    DATA_AND_COMPUTE = "data_and_compute"


class IndustryTag(StrEnum):
    GOVERNMENT = "government"
    FINANCE = "finance"
    MANUFACTURING = "manufacturing"
    ENERGY = "energy"
    TRANSPORT = "transport"
    HEALTHCARE = "healthcare"
    EDUCATION = "education"
    TELECOM = "telecom"
    INTERNET = "internet"
    RETAIL = "retail"
    GENERAL = "general"


class VerificationStatus(StrEnum):
    OFFICIAL_CONFIRMED = "official_confirmed"
    OFFICIAL_LINKED = "official_linked"
    MULTI_SOURCE_CONFIRMED = "multi_source_confirmed"
    MEDIA_ONLY = "media_only"
    RUMOR_OR_PREDICTION = "rumor_or_prediction"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class CaseCompleteness(StrEnum):
    NOT_CASE = "not_case"
    CASE_LEAD = "case_lead"
    PARTIAL_CASE = "partial_case"
    FULL_CASE = "full_case"


class LifecycleState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    PAUSED = "paused"


class SourceRole(StrEnum):
    OFFICIAL_PRODUCT = "official_product"
    OFFICIAL_POLICY = "official_policy"
    OFFICIAL_INDUSTRY = "official_industry"
    OPPORTUNITY_AND_AWARD_HUB = "opportunity_and_award_hub"
    OFFICIAL_CASE_HUB = "official_case_hub"
    REPORT_HUB = "report_hub"
    MEDIA_DISCOVERY = "media_discovery"
    FALLBACK = "fallback"


class CrawlMode(StrEnum):
    RSS = "rss"
    HTML_LIST = "html_list"
    SINGLE_PAGE_CHANGELOG = "single_page_changelog"
    DOCUMENT_HUB = "document_hub"
    CASE_HUB = "case_hub"
    API = "api"
    CUSTOM = "custom"
    RSSHUB = "rsshub"


class ReviewPolicy(StrEnum):
    AUTO_PUBLISH = "auto_publish"
    REVIEW_ON_LOW_CONFIDENCE = "review_on_low_confidence"
    ALWAYS_REVIEW = "always_review"
    NEVER_PUBLISH = "never_publish"


class ImplementationStatus(StrEnum):
    READY = "ready"
    NEEDS_CUSTOM_COLLECTOR = "needs_custom_collector"
    BLOCKED_BY_JAVASCRIPT = "blocked_by_javascript"
    RESEARCH_NEEDED = "research_needed"


class DiscoveryStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    NEEDS_CONFIGURATION = "needs_configuration"
    NEEDS_CUSTOM_COLLECTOR = "needs_custom_collector"
    BLOCKED = "blocked"
    UNREACHABLE = "unreachable"


class CrawlStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"

    # Source compatibility for callers compiled against the stage-one names.
    SUCCEEDED = SUCCESS
    PARTIAL = PARTIAL_SUCCESS


class RunTrigger(StrEnum):
    LEGACY_MANUAL = "legacy_manual"
    MANUAL_WEB = "manual_web"
    MANUAL_CLI = "manual_cli"
    SCHEDULED = "scheduled"


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"
