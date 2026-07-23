"""Source-role based verification without persistence or cross-site clustering."""

import re
from urllib.parse import urlsplit

from app.domain.collection import CollectedItem
from app.domain.enums import (
    ReviewPolicy,
    ReviewStatus,
    SourceRole,
    VerificationStatus,
)
from app.domain.models import Source
from app.domain.taxonomy import VerificationResult
from app.utils.url import canonicalize_url

_RUMOR = re.compile(r"据悉|消息称|知情人士|或将|预计|传闻|可能推出|计划发布|市场消息", re.I)


class VerificationService:
    """Assign verification/review defaults and validate any supplied official URL."""

    def verify(self, item: CollectedItem, source: Source) -> VerificationResult:
        content = f"{item.title} {item.summary or ''}"
        raw_official = item.extra.get("official_url")
        official_url = canonicalize_url(raw_official) if isinstance(raw_official, str) else None
        origin_publisher = (
            str(item.extra["origin_publisher"]).strip()[:500]
            if isinstance(item.extra.get("origin_publisher"), str)
            and str(item.extra["origin_publisher"]).strip()
            else None
        )
        if source.source_role is SourceRole.MEDIA_DISCOVERY:
            discovery_url = item.canonical_url
            if _RUMOR.search(content):
                status = VerificationStatus.RUMOR_OR_PREDICTION
                rules = ("verification.rumor_phrase",)
            elif official_url is not None and _different_host(official_url, discovery_url):
                status = VerificationStatus.OFFICIAL_LINKED
                rules = ("verification.valid_official_url",)
            else:
                official_url = None
                status = VerificationStatus.MEDIA_ONLY
                rules = ("verification.media_only",)
            return VerificationResult(
                status,
                ReviewStatus.PENDING,
                discovery_url,
                official_url,
                origin_publisher,
                rules,
            )

        review = (
            ReviewStatus.NOT_REQUIRED
            if source.review_policy is ReviewPolicy.AUTO_PUBLISH
            else ReviewStatus.PENDING
        )
        return VerificationResult(
            VerificationStatus.OFFICIAL_CONFIRMED,
            review,
            None,
            item.canonical_url,
            origin_publisher or source.name,
            ("verification.official_source",),
        )


def _different_host(left: str, right: str) -> bool:
    return (urlsplit(left).hostname or "").casefold() != (urlsplit(right).hostname or "").casefold()
