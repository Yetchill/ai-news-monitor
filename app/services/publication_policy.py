"""Publication decisions kept separate from collection, admission and classification."""

from app.domain.enums import (
    LifecycleState,
    PrimaryType,
    ReviewPolicy,
    ReviewStatus,
    SourceRole,
    VerificationStatus,
)
from app.domain.models import Source
from app.domain.taxonomy import PublicationDecision, TaxonomyResult, VerificationResult

_TRUSTED = {
    VerificationStatus.OFFICIAL_CONFIRMED,
    VerificationStatus.OFFICIAL_LINKED,
    VerificationStatus.MULTI_SOURCE_CONFIRMED,
}


class PublicationPolicy:
    def decide(
        self,
        *,
        source: Source,
        admission_accepted: bool,
        taxonomy: TaxonomyResult,
        verification: VerificationResult,
    ) -> PublicationDecision:
        reasons: list[str] = []
        active = source.lifecycle_state is LifecycleState.ACTIVE and source.enabled
        allowed = (
            not source.allowed_primary_types
            or taxonomy.primary_type.value in source.allowed_primary_types
        )
        trusted = verification.verification_status in _TRUSTED
        reviewed = verification.review_status in {ReviewStatus.NOT_REQUIRED, ReviewStatus.APPROVED}
        substantive = taxonomy.primary_type not in {
            PrimaryType.UNCLASSIFIED,
            PrimaryType.INDUSTRY_SIGNAL,
        }
        policy_allows = source.review_policy is not ReviewPolicy.NEVER_PUBLISH
        for ok, reason in (
            (active, "source.not_active"),
            (admission_accepted, "admission.rejected"),
            (allowed, "source.primary_type_not_allowed"),
            (trusted, "verification.not_trusted"),
            (reviewed, "review.not_approved"),
            (substantive, "taxonomy.not_formal"),
            (policy_allows, "source.never_publish"),
        ):
            if not ok:
                reasons.append(reason)
        base = not reasons
        industry_leads = (
            active
            and admission_accepted
            and (
                source.source_role is SourceRole.MEDIA_DISCOVERY
                or taxonomy.primary_type is PrimaryType.INDUSTRY_SIGNAL
                or verification.review_status is ReviewStatus.PENDING
            )
        )
        return PublicationDecision(
            leadership_homepage=base and source.homepage_visible,
            formal_export=base and source.export_visible,
            industry_leads=industry_leads,
            review_queue=active
            and admission_accepted
            and verification.review_status is ReviewStatus.PENDING,
            reasons=tuple(reasons),
        )
