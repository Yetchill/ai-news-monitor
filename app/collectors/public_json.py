"""Collect public HTTP GET JSON API endpoints with bounded extraction."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from app.domain.collection import CollectContext, CollectedItem, Fetcher, FetchResult
from app.utils.url import canonicalize_url

_BYTES_PER_MB = 1_048_576
_DEFAULT_RESPONSE_LIMIT_BYTES = 6 * _BYTES_PER_MB
_HARD_RESPONSE_LIMIT_BYTES = 20 * _BYTES_PER_MB
_DEFAULT_MAX_ITEMS = 50
_HARD_MAX_ITEMS = 200


class PublicJsonCollector:
    """Read a public GET JSON endpoint under explicit size and item bounds."""

    name = "public_json"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response_limit = _clamp_response_limit(context.config)
        response = await self._fetcher.fetch(
            _build_url(context.source_url, context.config),
            headers={"Accept": "application/json"},
        )
        raw = await _read_bounded(response, response_limit)
        data = cast(object, json.loads(raw))

        max_items = max(
            1,
            min(_integer(context.config.get("max_items"), _DEFAULT_MAX_ITEMS), _HARD_MAX_ITEMS),
        )
        items_field = _text(context.config.get("items_field")) or "articles"
        link_template = _text(context.config.get("link_template"))
        date_field = _text(context.config.get("date_field")) or "date"
        date_nested_in = _text(context.config.get("date_nested_in"))
        date_format = _text(context.config.get("date_format"))

        raw_items = _extract_items(data, items_field)
        collected: list[CollectedItem] = []
        for value in raw_items:
            if len(collected) >= max_items:
                break
            if not isinstance(value, Mapping):
                continue
            entry = cast(Mapping[str, object], value)
            title = _title(entry)
            if title is None:
                continue
            detail_url = _detail_url(entry, link_template, context.source_url)
            if detail_url is None:
                continue
            published_at = _parse_date(
                entry,
                date_field=date_field,
                date_nested_in=date_nested_in,
                date_format=date_format,
            )
            summary = _text(entry.get("summary")) or _text(entry.get("article_summary"))
            collected.append(
                CollectedItem(
                    title=title,
                    original_url=detail_url,
                    canonical_url=detail_url,
                    published_at=published_at,
                    summary=summary,
                    extra={"collector": "public_json"},
                )
            )
        return collected


def _clamp_response_limit(config: Mapping[str, object]) -> int:
    value = _integer(config.get("response_limit_bytes"), _DEFAULT_RESPONSE_LIMIT_BYTES)
    return max(1024, min(value, _HARD_RESPONSE_LIMIT_BYTES))


def _build_url(base_url: str, config: Mapping[str, object]) -> str:
    query = _text(config.get("query_params"))
    if not query:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{query}"


async def _read_bounded(response: FetchResult, limit_bytes: int) -> str:
    raw = response.text
    byte_size = len(raw.encode("utf-8"))
    if byte_size > limit_bytes:
        raise ValueError(
            f"JSON response body is {byte_size} bytes which exceeds the "
            f"limit of {limit_bytes} bytes"
        )
    return raw


def _extract_items(data: object, items_field: str) -> Sequence[object]:
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return cast(Sequence[object], data)
    if not isinstance(data, Mapping):
        return ()
    current: object = cast(object, data)
    for part in items_field.split("."):
        mapped = current
        if not isinstance(mapped, Mapping):
            return ()
        current = cast(Mapping[str, object], mapped).get(part)
    if isinstance(current, Mapping):
        cm = cast(Mapping[str, object], current)
        for key in ("list", "items", "articles", "data"):
            candidate = cm.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                return cast(Sequence[object], candidate)
    if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
        return cast(Sequence[object], current)
    return ()


def _title(entry: Mapping[str, object]) -> str | None:
    for key in ("article_title", "title", "name"):
        value = _text(entry.get(key))
        if value:
            return value
    return None


def _detail_url(
    entry: Mapping[str, object],
    link_template: str | None,
    source_url: str,
) -> str | None:
    if isinstance(link_template, str) and link_template:
        path = _text(entry.get("path")) or _text(entry.get("slug")) or _text(entry.get("id"))
        if not path:
            return None
        detail = link_template.replace("{path}", path).replace("{id}", path)
    else:
        for key in ("article_url", "url", "link", "html_url"):
            raw_url = _text(entry.get(key))
            if raw_url:
                detail = raw_url
                break
        else:
            path = _text(entry.get("path")) or _text(entry.get("slug"))
            if not path:
                return None
            base = source_url.rstrip("/")
            detail = f"{base}/{path}"
    return canonicalize_url(detail)


def _parse_date(
    entry: Mapping[str, object],
    *,
    date_field: str,
    date_nested_in: str | None,
    date_format: str | None,
) -> datetime | None:
    target: object
    if date_nested_in:
        nested = entry.get(date_nested_in)
        if not isinstance(nested, Mapping):
            return None
        target = cast(Mapping[str, object], nested).get(date_field)
    else:
        for key in ("publish_time", "published_at", "ctime"):
            alt = entry.get(key)
            if isinstance(alt, (int, float)) and not isinstance(alt, bool):
                return _from_timestamp_ms(alt)
            if isinstance(alt, str) and alt.strip():
                target = alt
                break
        else:
            target = entry.get(date_field)

    if target is None:
        return None

    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return _from_timestamp_ms(target)

    if not isinstance(target, str) or not target.strip():
        return None
    raw = target.strip()

    if date_format == "iso":
        return _from_iso(raw)
    if date_format == "timestamp_ms" and raw.isdecimal():
        return _from_timestamp_ms(raw)

    parsed = _from_iso(raw)
    if parsed is not None:
        return parsed
    return _from_timestamp_ms(raw)


def _from_iso(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def _from_timestamp_ms(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str) and value.strip().isdecimal():
        ts = int(value.strip())
        if ts > 0:
            return datetime.fromtimestamp(ts / 1000, tz=UTC)
    return None


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
