"""Final normalization boundary between collectors and durable intelligence items."""

import json
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import cast

from app.domain.collection import CollectedItem
from app.utils.url import canonicalize_url, is_http_url

INTERNAL_DISCOVERIES_KEY = "_source_discoveries"


class ItemNormalizationError(ValueError):
    """Raised when one collected record cannot safely be persisted."""


def normalize_collected_item(
    item: CollectedItem,
    *,
    keep_query_params: Collection[str] | None = None,
) -> CollectedItem:
    title = " ".join(item.title.split())
    if not title:
        raise ItemNormalizationError("item title is empty")

    original_url = item.original_url.strip()
    if not is_http_url(original_url):
        raise ItemNormalizationError("item URL must be an absolute HTTP(S) URL")
    canonical_url = canonicalize_url(original_url, keep_query_params=keep_query_params)
    if canonical_url is None:
        raise ItemNormalizationError("item URL cannot be canonicalized")

    summary = " ".join(item.summary.split()) if item.summary else None
    summary = summary or None
    published_at = _as_utc(item.published_at)
    extra = _json_object(item.extra)
    extra.pop(INTERNAL_DISCOVERIES_KEY, None)
    return CollectedItem(
        title=title,
        original_url=original_url,
        canonical_url=canonical_url,
        published_at=published_at,
        summary=summary,
        extra=extra,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_object(value: object) -> dict[str, object]:
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
        decoded = cast(object, json.loads(serialized))
    except (TypeError, ValueError) as exc:
        raise ItemNormalizationError("item extra must be JSON serializable") from exc
    if not isinstance(decoded, Mapping):
        raise ItemNormalizationError("item extra must be a JSON object with string keys")
    mapping = cast(Mapping[object, object], decoded)
    if not all(isinstance(key, str) for key in mapping):
        raise ItemNormalizationError("item extra must be a JSON object with string keys")
    return {cast(str, key): value for key, value in mapping.items()}
