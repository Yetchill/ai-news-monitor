"""Bounded HTML list collection using selectors or filtered links."""

import json
import re
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.utils.dates import parse_datetime
from app.utils.url import canonicalize_url, resolve_url

DEFAULT_EXCLUDE_TEXT = (
    "登录",
    "注册",
    "联系我们",
    "关于我们",
    "成员介绍",
    "组织架构",
    "活动日历",
    "招聘",
    "login",
    "register",
    "contact us",
)
ASSET_SUFFIXES = (
    ".7z",
    ".avi",
    ".css",
    ".gif",
    ".jpeg",
    ".jpg",
    ".js",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".rar",
    ".svg",
    ".webp",
    ".zip",
)
DATE_PATTERN = re.compile(
    r"(?:19|20)\d{2}\s*(?:[-/.年])\s*\d{1,2}\s*(?:[-/.月])\s*\d{1,2}(?:\s*日)?"
)


@dataclass(frozen=True, slots=True)
class _HTMLConfig:
    mode: str
    max_pages: int
    max_depth: int
    max_items: int
    allowed_domains: tuple[str, ...]
    allow_subdomains: bool
    include_text: tuple[str, ...]
    exclude_text: tuple[str, ...]
    include_url: tuple[str, ...]
    exclude_url: tuple[str, ...]
    keep_query_params: tuple[str, ...] | None
    item_selector: str | None
    title_selector: str | None
    link_selector: str | None
    date_selector: str | None
    ancestor_date_selector: str | None
    summary_selector: str | None
    pagination_selector: str | None
    embedded_title_key: str | None
    embedded_link_key: str | None
    embedded_link_template: str | None
    filter_selector_items: bool


class HTMLListCollector:
    """Collect entries from explicitly bounded list pages, never from detail pages."""

    name = "html_list"

    def __init__(self, fetcher: Fetcher, *, clock: Callable[[], datetime] | None = None) -> None:
        self._fetcher = fetcher
        self._clock = clock or (lambda: datetime.now(UTC))

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        config = _read_config(context)
        pending: deque[tuple[str, int]] = deque([(context.source_url, 0)])
        visited: set[str] = set()
        scheduled: set[str] = set()
        collected: dict[str, CollectedItem] = {}

        start_page = canonicalize_url(
            context.source_url,
            keep_query_params=config.keep_query_params,
        )
        if start_page is not None:
            scheduled.add(start_page)

        while pending and len(visited) < config.max_pages and len(collected) < config.max_items:
            page_url, depth = pending.popleft()
            canonical_page = canonicalize_url(
                page_url,
                keep_query_params=config.keep_query_params,
            )
            if canonical_page is None or canonical_page in visited:
                continue
            if not _is_allowed_domain(canonical_page, config):
                continue
            visited.add(canonical_page)

            response = await self._fetcher.fetch(page_url)
            soup = BeautifulSoup(response.content, "lxml")
            collected_at = self._clock()
            if config.mode == "selectors":
                page_items = _selector_items(
                    soup,
                    response.url,
                    config,
                    _embedded_link_map(response.text, config, config.max_items),
                    config.max_items - len(collected),
                    collected_at,
                )
            else:
                page_items = _filtered_link_items(
                    soup,
                    response.url,
                    config,
                    config.max_items - len(collected),
                    collected_at,
                )
            for item in page_items:
                collected.setdefault(item.canonical_url, item)

            if (
                len(collected) < config.max_items
                and depth < config.max_depth
                and config.pagination_selector
            ):
                for pagination_url in _pagination_urls(
                    soup,
                    response.url,
                    config.pagination_selector,
                    config,
                    excluded=visited | scheduled,
                    limit=config.max_pages - len(scheduled),
                ):
                    if pagination_url not in visited and pagination_url not in scheduled:
                        if len(scheduled) >= config.max_pages:
                            break
                        scheduled.add(pagination_url)
                        pending.append((pagination_url, depth + 1))

        return list(collected.values())


def _read_config(context: CollectContext) -> _HTMLConfig:
    root = context.config
    discovery = _mapping(root.get("discovery"))
    extraction = _mapping(root.get("extraction"))
    mode = _text(discovery.get("mode")) or _text(root.get("mode")) or "link_filter"
    if mode not in {"selectors", "link_filter"}:
        raise ValueError("HTML collector mode must be 'selectors' or 'link_filter'")

    source_host = (urlsplit(context.source_url).hostname or "").lower()
    allowed_domains = _strings(root.get("allowed_domains")) or (source_host,)
    keep_params_value = root.get("keep_query_params")
    keep_query_params = _strings(keep_params_value) if keep_params_value is not None else None
    exclude_text = _strings(discovery.get("exclude_text"))
    return _HTMLConfig(
        mode=mode,
        max_pages=max(1, min(_integer(discovery.get("max_pages"), 20), 100)),
        max_depth=max(0, min(_integer(discovery.get("max_depth"), 1), 3)),
        max_items=max(
            1,
            min(
                _integer(discovery.get("max_items"), _integer(root.get("max_items"), 1000)),
                10_000,
            ),
        ),
        allowed_domains=tuple(domain.lower().lstrip(".") for domain in allowed_domains if domain),
        allow_subdomains=_boolean(root.get("allow_subdomains"), False),
        include_text=_strings(discovery.get("include_text")),
        exclude_text=exclude_text or DEFAULT_EXCLUDE_TEXT,
        include_url=_strings(discovery.get("include_url_patterns")),
        exclude_url=_strings(discovery.get("exclude_url_patterns")),
        keep_query_params=keep_query_params,
        item_selector=_text(extraction.get("item_selector")),
        title_selector=_text(extraction.get("title_selector")),
        link_selector=_text(extraction.get("link_selector")),
        date_selector=_text(extraction.get("date_selector")),
        ancestor_date_selector=_text(extraction.get("ancestor_date_selector")),
        summary_selector=_text(extraction.get("summary_selector")),
        pagination_selector=_text(discovery.get("pagination_selector")),
        embedded_title_key=_text(extraction.get("embedded_title_key")),
        embedded_link_key=_text(extraction.get("embedded_link_key")),
        embedded_link_template=_text(extraction.get("embedded_link_template")),
        filter_selector_items=_boolean(root.get("filter_selector_items"), False),
    )


def _selector_items(
    soup: BeautifulSoup,
    page_url: str,
    config: _HTMLConfig,
    embedded_links: Mapping[str, str],
    limit: int,
    collected_at: datetime,
) -> list[CollectedItem]:
    if not config.item_selector:
        raise ValueError("selectors mode requires extraction.item_selector")
    items: list[CollectedItem] = []
    for node in soup.select(config.item_selector):
        if len(items) >= limit:
            break
        try:
            title_node = node.select_one(config.title_selector) if config.title_selector else None
            link_node = node.select_one(config.link_selector) if config.link_selector else None
            if link_node is None and node.name == "a":
                link_node = node
            if link_node is None:
                link_node = node.select_one("a[href]")
            if title_node is None:
                title_node = link_node
            title = _node_text(title_node)
            href_value = link_node.get("href") if link_node is not None else None
            href = href_value if isinstance(href_value, str) else embedded_links.get(title)
            if href is None:
                continue
            url = resolve_url(page_url, href, keep_query_params=config.keep_query_params)
            if url is None or not _is_allowed_domain(url, config) or _url_is_excluded(url, config):
                continue
            if not _valid_title(
                title, url, config, apply_include_rules=config.filter_selector_items
            ):
                continue
            date_text = (
                _node_text(node.select_one(config.date_selector)) if config.date_selector else None
            )
            if not date_text and config.ancestor_date_selector:
                date_text = _ancestor_text(node, config.ancestor_date_selector)
            summary = (
                _node_text(node.select_one(config.summary_selector))
                if config.summary_selector
                else None
            )
            items.append(
                CollectedItem(
                    title=title,
                    original_url=url,
                    canonical_url=url,
                    published_at=parse_datetime(date_text, relative_base=collected_at),
                    summary=summary,
                    extra={
                        "link_type": _link_type(page_url, url),
                        "original_time_text": date_text,
                    },
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return items


def _ancestor_text(node: Tag, selector: str) -> str | None:
    """Return the nearest group-level date without reaching unrelated page sections."""

    for parent in node.parents:
        matched = parent.select_one(selector)
        if matched is not None:
            return _node_text(matched)
        if parent.name in {"main", "body", "html"}:
            break
    return None


def _embedded_link_map(html: str, config: _HTMLConfig, limit: int) -> Mapping[str, str]:
    """Read configured title/link pairs from JSON-like data embedded in HTML scripts."""

    if not config.embedded_title_key or not config.embedded_link_key:
        return {}
    unescaped = html.replace(r"\"", '"')
    title_key = re.escape(config.embedded_title_key)
    link_key = re.escape(config.embedded_link_key)
    string_value = r'"(?P<link>(?:\\.|[^"])*)"'
    raw_value = rf"(?:{string_value}|(?P<link_number>\d+))"
    pattern = re.compile(
        rf'"{title_key}":"(?P<title>(?:\\.|[^"])*)"'
        rf'.{{0,3000}}?"{link_key}":{raw_value}',
        re.DOTALL,
    )
    reverse_pattern = re.compile(
        rf'"{link_key}":{raw_value}'
        rf'.{{0,3000}}?"{title_key}":"(?P<title>(?:\\.|[^"])*)"',
        re.DOTALL,
    )
    links: dict[str, str] = {}
    for match in (*pattern.finditer(unescaped), *reverse_pattern.finditer(unescaped)):
        if len(links) >= limit:
            break
        title = _decode_json_string(match.group("title"))
        raw_link = match.group("link") or match.group("link_number")
        link = _decode_json_string(raw_link) if match.group("link") else raw_link
        if link and config.embedded_link_template:
            link = config.embedded_link_template.replace("{value}", link)
        if title and link:
            links[title] = link
    return links


def _decode_json_string(value: str) -> str | None:
    try:
        decoded = json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, str) and decoded else None


def _filtered_link_items(
    soup: BeautifulSoup,
    page_url: str,
    config: _HTMLConfig,
    limit: int,
    collected_at: datetime,
) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for anchor in soup.select("a[href]"):
        if len(items) >= limit:
            break
        try:
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            url = resolve_url(page_url, href, keep_query_params=config.keep_query_params)
            if url is None or not _is_allowed_domain(url, config) or _url_is_excluded(url, config):
                continue
            title = _node_text(anchor)
            if not _valid_title(title, url, config, apply_include_rules=True):
                continue
            items.append(
                CollectedItem(
                    title=title,
                    original_url=url,
                    canonical_url=url,
                    published_at=parse_datetime(_nearby_date(anchor), relative_base=collected_at),
                    summary=_nearby_summary(anchor, title),
                    extra={"link_type": _link_type(page_url, url)},
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return items


def _pagination_urls(
    soup: BeautifulSoup,
    page_url: str,
    selector: str,
    config: _HTMLConfig,
    *,
    excluded: Collection[str],
    limit: int,
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for node in soup.select(selector):
        if len(urls) >= limit:
            break
        try:
            target = node if node.name == "a" else node.select_one("a[href]")
            if target is None:
                continue
            href = target.get("href")
            if not isinstance(href, str):
                continue
            resolved = resolve_url(page_url, href, keep_query_params=config.keep_query_params)
            if (
                resolved
                and resolved not in excluded
                and resolved not in seen
                and _is_allowed_domain(resolved, config)
            ):
                seen.add(resolved)
                urls.append(resolved)
        except (AttributeError, TypeError, ValueError):
            continue
    return urls


def _valid_title(title: str, url: str, config: _HTMLConfig, *, apply_include_rules: bool) -> bool:
    normalized = " ".join(title.split()).strip()
    if len(normalized) < 4 or not re.search(r"[A-Za-z\u3400-\u9fff]", normalized):
        return False
    lowered_title = normalized.casefold()
    lowered_url = url.casefold()
    if any(term.casefold() in lowered_title for term in config.exclude_text):
        return False
    if apply_include_rules and (config.include_text or config.include_url):
        return any(term.casefold() in lowered_title for term in config.include_text) or any(
            pattern.casefold() in lowered_url for pattern in config.include_url
        )
    return True


def _link_type(page_url: str, item_url: str) -> str:
    page_host = (urlsplit(page_url).hostname or "").casefold()
    item_host = (urlsplit(item_url).hostname or "").casefold()
    return "same_site" if page_host == item_host else "external"


def _url_is_excluded(url: str, config: _HTMLConfig) -> bool:
    lowered = url.casefold()
    path = urlsplit(url).path.casefold()
    return path.endswith(ASSET_SUFFIXES) or any(
        pattern.casefold() in lowered for pattern in config.exclude_url
    )


def _is_allowed_domain(url: str, config: _HTMLConfig) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    for domain in config.allowed_domains:
        if hostname == domain or (config.allow_subdomains and hostname.endswith(f".{domain}")):
            return True
    return False


def _nearby_date(anchor: Tag) -> str | None:
    node: Tag | None = anchor
    for _ in range(3):
        if node is None:
            break
        match = DATE_PATTERN.search(node.get_text(" ", strip=True))
        if match:
            return match.group(0)
        node = node.parent if isinstance(node.parent, Tag) else None
    return None


def _nearby_summary(anchor: Tag, title: str) -> str | None:
    parent = anchor.parent if isinstance(anchor.parent, Tag) else None
    if parent is None:
        return None
    candidate = parent.select_one(".summary, .description, .desc, p")
    text = _node_text(candidate)
    return text if text and text != title and len(text) > 8 else None


def _node_text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    mapping = cast(Mapping[object, object], value)
    return {str(key): item for key, item in mapping.items()}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    sequence = cast(Sequence[object], value)
    return tuple(item.strip() for item in sequence if isinstance(item, str) and item.strip())


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default
