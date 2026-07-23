"""Collect huxiu article list from public Nuxt 3 JSON embedded in the channel page."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast

from bs4 import BeautifulSoup

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.url import canonicalize_url


class HuxiuCollector:
    """Read the public Nuxt 3 page payload without signed or authenticated APIs."""

    name = "huxiu"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        soup = BeautifulSoup(response.content, "lxml")
        payload_node = soup.select_one("script#__NUXT_DATA__")
        if payload_node is None or not payload_node.string:
            return []
        try:
            payload = cast(object, json.loads(payload_node.string))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            return []
        data_array = cast(Sequence[object], payload)

        def resolve(idx: object) -> object:
            if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(data_array):
                return data_array[idx]
            return idx

        articles = _huxiu_articles(data_array, resolve)
        max_items = max(1, min(_integer(context.config.get("max_items"), 20), 200))
        items: list[CollectedItem] = []
        seen_aids: set[str] = set()
        for article in articles:
            if len(items) >= max_items:
                break
            aid = _identifier(resolve(article.get("aid")))
            title = _text(resolve(article.get("title")))
            dateline = _timestamp(resolve(article.get("dateline")))
            if aid is None or title is None:
                continue
            if aid in seen_aids:
                continue
            seen_aids.add(aid)
            raw_url = _text(resolve(article.get("url")))
            url = canonicalize_url(
                _clean_url(raw_url)
                or f"https://www.huxiu.com/article/{aid}.html"
            )
            if url is None:
                continue
            published_at = datetime.fromtimestamp(dateline, tz=UTC) if dateline else None
            items.append(
                CollectedItem(
                    title=title,
                    original_url=url,
                    canonical_url=url,
                    published_at=published_at,
                    summary=None,
                    extra={"public_payload": "__NUXT_DATA__", "article_id": aid},
                )
            )
        return items


def _huxiu_articles(
    data_array: Sequence[object], resolve: Callable[[object], object]
) -> Sequence[Mapping[str, object]]:
    for channel_item in data_array:
        if not isinstance(channel_item, dict):
            continue
        channel = cast(dict[str, object], channel_item)
        datalist_ref = channel.get("datalist")
        if datalist_ref is None:
            continue
        datalist_raw = resolve(datalist_ref)
        if not isinstance(datalist_raw, Sequence) or isinstance(datalist_raw, (str, bytes)):
            continue
        datalist = cast(Sequence[object], datalist_raw)
        articles: list[Mapping[str, object]] = []
        for ref in datalist:
            obj = resolve(ref)
            if isinstance(obj, Mapping):
                articles.append(cast(Mapping[str, object], obj))
        if articles:
            return articles
    return []


def _clean_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[?&]type=\w+", "", raw)
    return cleaned or None


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


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
