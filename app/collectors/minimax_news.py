"""Collect MiniMax news from the public /api/news JSON endpoint."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.dates import parse_datetime
from app.utils.url import canonicalize_url, resolve_url


class MiniMaxNewsCollector:
    """Read the public /api/news JSON without signed or authenticated APIs."""

    name = "minimax_news"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        try:
            payload = cast(object, json.loads(response.text))
        except json.JSONDecodeError:
            return []
        entries = _data(payload)
        max_items = max(1, min(_integer(context.config.get("max_items"), 50), 100))
        items: list[CollectedItem] = []
        seen_slugs: set[str] = set()
        for value in entries:
            if len(items) >= max_items:
                break
            if not isinstance(value, Mapping):
                continue
            entry = cast(Mapping[str, object], value)
            slug = _text(entry.get("slug"))
            title = _text(entry.get("title"))
            if slug is None or title is None:
                continue
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            link = resolve_url(
                "https://www.minimaxi.com/news/",
                slug,
            )
            if link is None:
                continue
            canonical = canonicalize_url(link)
            if canonical is None:
                continue
            published_at = _parse_date(entry.get("publishDate"))
            summary = _text(entry.get("summary"))
            tags = _strings(entry.get("tags"))
            items.append(
                CollectedItem(
                    title=title,
                    original_url=canonical,
                    canonical_url=canonical,
                    published_at=published_at,
                    summary=summary,
                    extra={"public_payload": "api/news", "slug": slug, "tags": tags},
                )
            )
        return items


def _data(payload: object) -> Sequence[object]:
    if not isinstance(payload, Mapping):
        return ()
    data = cast(Mapping[str, object], payload).get("data")
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        return cast(Sequence[object], data)
    return ()


def _parse_date(value: object) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        return parse_datetime(value.strip())
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    sequence = cast(Sequence[object], value)
    return tuple(item.strip() for item in sequence if isinstance(item, str) and item.strip())


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
