"""RSS and Atom list collector."""

from collections.abc import Mapping, Sequence
from typing import cast

import feedparser  # pyright: ignore[reportMissingTypeStubs]
from bs4 import BeautifulSoup

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.dates import parse_datetime
from app.utils.url import canonicalize_url


class RSSCollector:
    """Collect feed metadata without visiting entry detail pages."""

    name = "rss"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        parsed = _parse_feed(response.content)
        entries_value = parsed.get("entries", [])
        if not isinstance(entries_value, Sequence) or isinstance(entries_value, (str, bytes)):
            return []

        include_categories = _strings(context.config.get("include_categories"))
        exclude_categories = _strings(context.config.get("exclude_categories"))

        items: list[CollectedItem] = []
        seen_urls: set[str] = set()
        max_items = max(1, min(_integer(context.config.get("max_items"), 1000), 10_000))
        entries = cast(Sequence[object], entries_value)
        for raw_entry in entries:
            if len(items) >= max_items:
                break
            try:
                if not isinstance(raw_entry, Mapping):
                    continue
                entry = cast(Mapping[str, object], raw_entry)
                title = _string(entry.get("title"))
                link = _string(entry.get("link"))
                if not title or not link:
                    continue
                plain_title = _plain_text(title)
                if not plain_title:
                    continue
                if include_categories or exclude_categories:
                    entry_categories = _entry_categories(entry)
                    if include_categories and not any(
                        cat in include_categories for cat in entry_categories
                    ):
                        continue
                    if any(cat in exclude_categories for cat in entry_categories):
                        continue
                canonical_url = canonicalize_url(link, base_url=response.url)
                if canonical_url is None or canonical_url in seen_urls:
                    continue
                seen_urls.add(canonical_url)
                summary_value = _string(entry.get("summary")) or _string(entry.get("description"))
                summary = _plain_text(summary_value)
                published = (
                    _string(entry.get("published"))
                    or _string(entry.get("updated"))
                    or _string(entry.get("created"))
                )
                items.append(
                    CollectedItem(
                        title=plain_title,
                        original_url=canonical_url,
                        canonical_url=canonical_url,
                        published_at=parse_datetime(published),
                        summary=summary,
                    )
                )
            except Exception:
                # Feeds in the wild often contain one malformed entry. Keep the rest usable.
                continue
        return items


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _parse_feed(content: bytes) -> Mapping[str, object]:
    return cast(
        Mapping[str, object],
        feedparser.parse(content),  # pyright: ignore[reportUnknownMemberType]
    )


def _plain_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = BeautifulSoup(value, "lxml").get_text(" ", strip=True)
    return " ".join(text.split()) or None


def _entry_categories(entry: Mapping[str, object]) -> set[str]:
    categories: set[str] = set()
    tags_value = entry.get("tags")
    if isinstance(tags_value, Sequence) and not isinstance(tags_value, (str, bytes)):
        for tag in tags_value:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(tag, Mapping):
                cat = _string(cast(Mapping[str, object], tag).get("term"))
                if cat:
                    categories.add(cat)
    cat_value = entry.get("category")
    if isinstance(cat_value, str) and cat_value.strip():
        categories.add(cat_value.strip())
    return categories


def _strings(value: object) -> frozenset[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return frozenset(
            item.strip() for item in value if isinstance(item, str) and item.strip()  # pyright: ignore[reportUnknownVariableType]
        )
    return frozenset()


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
