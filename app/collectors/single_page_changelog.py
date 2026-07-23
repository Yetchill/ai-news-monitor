"""Bounded section collector for official single-page changelogs."""

import re
from collections.abc import Mapping
from hashlib import sha256
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.dates import parse_datetime
from app.utils.url import canonicalize_url

_DATE = re.compile(r"(?:19|20)\d{2}[年./-]\d{1,2}[月./-]\d{1,2}日?")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_ACTION = re.compile(r"发布|上线|升级|更新|新增|开源|退役|下线|降价|支持|修复|优化")


class SinglePageChangelogCollector:
    name = "single_page_changelog"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        response = await self._fetcher.fetch(context.source_url)
        soup = BeautifulSoup(response.content, "lxml")
        config = _mapping(context.config)
        selector = (
            _text(config.get("section_selector")) or "article h2, article h3, main h2, main h3"
        )
        max_items = min(200, max(1, _integer(config.get("max_items"), 20)))
        if _text(config.get("date_heading_selector")) and _text(config.get("entry_selector")):
            return _dated_entries(soup, response.url, config, max_items=max_items)
        items: list[CollectedItem] = []
        seen_keys: set[str] = set()
        for heading in soup.select(selector):
            if len(items) >= max_items:
                break
            title = " ".join(heading.get_text(" ", strip=True).split())
            if len(title) < 4:
                continue
            summary = _section_summary(heading)
            date_match = _DATE.search(f"{title} {summary or ''}")
            stable_text = f"{date_match.group(0) if date_match else ''}\n{title}".casefold()
            key = sha256(stable_text.encode("utf-8")).hexdigest()[:20]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            url = _entry_url(response.url, key)
            items.append(
                CollectedItem(
                    title=title,
                    original_url=url,
                    canonical_url=url,
                    published_at=parse_datetime(date_match.group(0) if date_match else None),
                    summary=summary,
                    extra={"changelog_entry_key": key, "detail_fetched": True},
                )
            )
        return items


def _dated_entries(
    soup: BeautifulSoup,
    page_url: str,
    config: Mapping[str, object],
    *,
    max_items: int,
) -> list[CollectedItem]:
    date_selector = _required_text(config, "date_heading_selector")
    entry_selector = _required_text(config, "entry_selector")
    content_selector = _text(config.get("content_selector"))
    root = soup.select_one(content_selector) if content_selector else soup
    if root is None:
        return []
    date_nodes = {id(node) for node in root.select(date_selector)}
    title_selector = _text(config.get("entry_title_selector"))
    item_date_selector = _text(config.get("entry_date_selector"))
    summary_selector = _text(config.get("entry_summary_selector"))
    title_cells = _integers(config.get("entry_title_cells"))
    date_cell = _optional_integer(config.get("entry_date_cell"))
    summary_cell = _optional_integer(config.get("entry_summary_cell"))
    link_cell = _optional_integer(config.get("entry_link_cell"))
    include_terms = _strings(config.get("include_entry_terms"))
    exclude_terms = _strings(config.get("exclude_entry_terms"))
    replacements = _string_mapping(config.get("entry_title_replacements"))
    append_summary = config.get("append_summary_when_title_lacks_action") is True
    current_date_text: str | None = None
    items: list[CollectedItem] = []
    seen_keys: set[str] = set()
    for node in root.select(f"{date_selector}, {entry_selector}"):
        if id(node) in date_nodes:
            current_date_text = _node_text(node)
            continue
        if len(items) >= max_items:
            break
        if current_date_text is None and date_cell is None and not item_date_selector:
            continue
        cells = node.select("th, td")
        title = (
            _cell_text(cells, title_cells) if title_cells else _selected_text(node, title_selector)
        )
        if not title:
            title = _node_text(node)
        raw_date = (
            _cell(cells, date_cell)
            if date_cell is not None
            else (_selected_text(node, item_date_selector) if item_date_selector else None)
        )
        summary = (
            _cell(cells, summary_cell)
            if summary_cell is not None
            else (_selected_text(node, summary_selector) if summary_selector else None)
        )
        if summary is None and node.name in {"h2", "h3", "h4"}:
            summary = _section_summary(node)
        normalized = " ".join(f"{title} {summary or ''}".split())
        if len(title) < 4 or any(
            term.casefold() in normalized.casefold() for term in exclude_terms
        ):
            continue
        if include_terms and not any(
            term.casefold() in normalized.casefold() for term in include_terms
        ):
            continue
        if append_summary and not _ACTION.search(title) and summary:
            suffix = re.split("[。\uff1b;\n]", summary, maxsplit=1)[0].strip()
            if suffix and suffix.casefold() not in title.casefold():
                title = f"{title}\uff1a{suffix[:100]}"
        for old, new in replacements.items():
            title = title.replace(old, new)
        date_text = _date_with_context(raw_date, current_date_text)
        published_at = parse_datetime(date_text)
        stable_text = f"{date_text or current_date_text}\n{title}".casefold()
        key = sha256(stable_text.encode("utf-8")).hexdigest()[:20]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cell_link = _cell_href(cells, link_cell) if link_cell is not None else None
        url = _entry_url(page_url, key)
        extra: dict[str, object] = {
            "changelog_entry_key": key,
            "detail_fetched": True,
            "original_time_text": raw_date or current_date_text,
        }
        if cell_link:
            extra["detail_link"] = cell_link
        items.append(
            CollectedItem(
                title=title[:500],
                original_url=url,
                canonical_url=url,
                published_at=published_at,
                summary=summary[:2_000] if summary else None,
                extra=extra,
            )
        )
    return items


def _section_summary(heading: Tag) -> str | None:
    parts: list[str] = []
    sibling = heading.next_sibling
    while sibling is not None and len(" ".join(parts)) < 2_000:
        if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3"}:
            break
        if isinstance(sibling, Tag):
            text = " ".join(sibling.get_text(" ", strip=True).split())
            if text:
                parts.append(text)
        sibling = sibling.next_sibling
    value = " ".join(parts).strip()
    return value[:2_000] or None


def _entry_url(page_url: str, key: str) -> str:
    split = urlsplit(page_url)
    query = dict(parse_qsl(split.query, keep_blank_values=True))
    query["entry"] = key
    value = urlunsplit(
        (split.scheme, split.netloc, split.path, urlencode(sorted(query.items())), "")
    )
    return canonicalize_url(value, keep_query_params=tuple(query)) or value


def _date_with_context(value: str | None, context: str | None) -> str:
    candidate = value or context or ""
    if not _YEAR.search(candidate) and context and (year := _YEAR.search(context)):
        candidate = f"{year.group(0)}年{candidate}"
    match = _DATE.search(candidate)
    return match.group(0) if match else candidate


def _selected_text(node: Tag, selector: str | None) -> str | None:
    selected = node.select_one(selector) if selector else node
    return _node_text(selected) or None


def _node_text(node: Tag | None) -> str:
    if node is None:
        return ""
    raw = node.get_text(" ", strip=True).replace("\u200b", "").replace("\ufeff", "")
    return " ".join(raw.split())


def _cell_href(cells: list[Tag], index: int) -> str | None:
    if index < 0 or index >= len(cells):
        return None
    anchor = cells[index].select_one("a[href]")
    if anchor is None:
        return None
    href = anchor.get("href")
    return href.strip() if isinstance(href, str) and href.strip() else None


def _cell(cells: list[Tag], index: int) -> str | None:
    if index < 0 or index >= len(cells):
        return None
    return _node_text(cells[index]) or None


def _cell_text(cells: list[Tag], indices: tuple[int, ...]) -> str:
    return " — ".join(value for index in indices if (value := _cell(cells, index)))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): child for key, child in cast(Mapping[object, object], value).items()}


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = _text(value.get(key))
    if result is None:
        raise ValueError(f"single-page changelog requires {key}")
    return result


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        child.strip()
        for child in cast(list[object], value)
        if isinstance(child, str) and child.strip()
    )


def _integers(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        child
        for child in cast(list[object], value)
        if isinstance(child, int) and not isinstance(child, bool)
    )


def _string_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): child
        for key, child in cast(Mapping[object, object], value).items()
        if isinstance(key, str) and key and isinstance(child, str) and child
    }


def _optional_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default
