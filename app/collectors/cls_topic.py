"""Collect a CLS topic from public JSON embedded in the topic page."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.url import canonicalize_url


class CLSTopicCollector:
    """Read the public Next.js page payload without signed or authenticated APIs."""

    name = "cls_topic"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        soup = BeautifulSoup(response.content, "lxml")
        payload_node = soup.select_one("script#__NEXT_DATA__")
        if payload_node is None or not payload_node.string:
            return []
        try:
            payload = cast(object, json.loads(payload_node.string))
        except json.JSONDecodeError:
            return []
        articles = _articles(payload)
        max_items = max(1, min(_integer(context.config.get("max_items"), 20), 100))
        items: list[CollectedItem] = []
        seen_ids: set[str] = set()
        for value in articles:
            if len(items) >= max_items:
                break
            if not isinstance(value, Mapping):
                continue
            article = cast(Mapping[str, object], value)
            article_id = _identifier(article.get("article_id"))
            raw_title = _text(article.get("article_title"))
            title = _headline(raw_title)
            timestamp = _timestamp(article.get("article_time"))
            if article_id is None or title is None or timestamp is None:
                continue
            if article_id in seen_ids:
                continue
            seen_ids.add(article_id)
            link = canonicalize_url(urljoin(response.url, f"/detail/{article_id}"))
            if link is None:
                continue
            summary = _text(article.get("article_brief"))
            if summary is None and raw_title != title:
                summary = raw_title
            items.append(
                CollectedItem(
                    title=title,
                    original_url=link,
                    canonical_url=link,
                    published_at=datetime.fromtimestamp(timestamp, tz=UTC),
                    summary=summary,
                    extra={"public_payload": "__NEXT_DATA__", "article_id": article_id},
                )
            )
        return items


def _articles(payload: object) -> Sequence[object]:
    current = payload
    for key in ("props", "pageProps", "data", "articles"):
        if not isinstance(current, Mapping):
            return ()
        current = cast(Mapping[str, object], current).get(key)
    if isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
        return cast(Sequence[object], current)
    return ()


def _identifier(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return value
    return None


def _timestamp(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _headline(value: object) -> str | None:
    title = _text(value)
    if title is None or not title.startswith("【"):
        return title
    closing = title.find("】", 1, 201)
    if closing < 5:
        return title
    headline = title[1:closing].strip()
    return headline or title


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
