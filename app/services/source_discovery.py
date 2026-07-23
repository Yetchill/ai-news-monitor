"""Bounded source discovery, collector preview, and temporary server-side state."""

import re
import secrets
from collections import Counter, OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import cast
from urllib.parse import urljoin, urlsplit

import feedparser  # pyright: ignore[reportMissingTypeStubs]
from bs4 import BeautifulSoup, Tag

from app.collectors.registry import CollectorRegistry
from app.domain.classification import Classifier
from app.domain.collection import CollectContext, Fetcher, FetchResult
from app.domain.enums import (
    Category,
    DiscoveryStatus,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceTier,
    SourceType,
)
from app.domain.models import Source
from app.domain.onboarding import DiscoveryResult, DiscoverySession, PreviewItem, PreviewResult
from app.fetchers.errors import FetchError, ForbiddenFetchError
from app.services.content_admission import ContentAdmissionPolicy
from app.services.item_normalization import ItemNormalizationError, normalize_collected_item
from app.services.source_url_security import SourceUrlGuard, SourceUrlSecurityError
from app.utils.url import canonicalize_url

COMMON_FEED_PATHS = ("/feed", "/rss", "/atom.xml", "/feed.xml")
HTML_SELECTOR_CANDIDATES = (
    ".news-list li",
    ".article-list li",
    ".post-list article",
    ".posts article",
    "main article",
    "h3:has(a[href])",
)
GITHUB_PART_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
GITHUB_RESERVED_OWNERS = {
    "collections",
    "events",
    "features",
    "login",
    "marketplace",
    "new",
    "organizations",
    "orgs",
    "search",
    "settings",
    "sponsors",
    "topics",
    "users",
}
DISCOVERY_TTL_SECONDS = 15 * 60
DISCOVERY_CACHE_SIZE = 256
MAX_PREVIEW_TITLE_LENGTH = 1000
MAX_PREVIEW_SUMMARY_LENGTH = 2000


class DiscoveryTokenError(ValueError):
    """A missing, forged, or expired temporary discovery token."""


class DiscoveryTokenStore:
    """Process-local bounded TTL store; responses and full HTML are never retained."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DISCOVERY_TTL_SECONDS,
        max_entries: int = DISCOVERY_CACHE_SIZE,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("token store limits must be positive")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, DiscoverySession]] = OrderedDict()
        self._claimed: set[str] = set()
        self._lock = Lock()

    def put(self, session: DiscoverySession) -> str:
        with self._lock:
            self._purge_expired()
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            token = secrets.token_urlsafe(32)
            while token in self._entries:
                token = secrets.token_urlsafe(32)
            self._entries[token] = (monotonic() + self._ttl, session)
            return token

    def get(self, token: str) -> DiscoverySession:
        if not token or len(token) > 128:
            raise DiscoveryTokenError("检测结果无效或已过期, 请重新检测。")
        with self._lock:
            self._purge_expired()
            entry = self._entries.get(token)
            if entry is None or token in self._claimed:
                raise DiscoveryTokenError("检测结果无效或已过期, 请重新检测。")
            self._entries.move_to_end(token)
            return entry[1]

    def claim(self, token: str) -> DiscoverySession:
        """Exclusively reserve a token until the operation succeeds or releases it."""

        if not token or len(token) > 128:
            raise DiscoveryTokenError("检测结果无效或已过期, 请重新检测。")
        with self._lock:
            self._purge_expired()
            entry = self._entries.get(token)
            if entry is None or token in self._claimed:
                raise DiscoveryTokenError("检测结果无效或已过期, 请重新检测。")
            self._claimed.add(token)
            return entry[1]

    def release(self, token: str) -> None:
        """Make a failed operation's still-valid token available again."""

        with self._lock:
            if token in self._entries:
                self._claimed.discard(token)

    def discard(self, token: str) -> None:
        with self._lock:
            self._entries.pop(token, None)
            self._claimed.discard(token)

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired()
            return len(self._entries)

    def _purge_expired(self) -> None:
        now = monotonic()
        expired = [token for token, (deadline, _) in self._entries.items() if deadline <= now]
        for token in expired:
            self._entries.pop(token, None)
            self._claimed.discard(token)


class SourceDiscoveryService:
    def __init__(self, fetcher: Fetcher, guard: SourceUrlGuard) -> None:
        self._fetcher = fetcher
        self._guard = guard

    async def discover(self, input_url: str) -> DiscoveryResult:
        tested_at = datetime.now(UTC)
        normalized = self._guard.normalize(input_url)
        try:
            normalized = await self._guard.validate(normalized)
        except SourceUrlSecurityError:
            raise

        github = _github_result(normalized, tested_at)
        if github is not None:
            return github

        try:
            response = await self._fetcher.fetch(normalized)
        except ForbiddenFetchError:
            return _failed_result(
                normalized,
                DiscoveryStatus.BLOCKED,
                "网站拒绝公开访问, 未尝试绕过访问限制。",
                tested_at,
            )
        except FetchError:
            return _failed_result(
                normalized,
                DiscoveryStatus.UNREACHABLE,
                "当前无法安全访问该网址, 请稍后重试。",
                tested_at,
            )

        if _is_feed(response):
            return _feed_result(await self._guard.validate(response.url), tested_at, direct=True)
        if not _looks_like_html(response):
            return _failed_result(
                normalized,
                DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR,
                "响应不是可识别的 RSS、Atom 或普通 HTML 列表页。",
                tested_at,
            )

        soup = BeautifulSoup(response.content, "lxml")
        for feed_url in _alternate_feed_urls(soup, response.url):
            try:
                safe_url = await self._guard.validate(feed_url)
                feed_response = await self._fetcher.fetch(safe_url)
            except (FetchError, SourceUrlSecurityError):
                continue
            if _is_feed(feed_response):
                return _feed_result(
                    await self._guard.validate(feed_response.url), tested_at, direct=False
                )

        origin = f"{urlsplit(response.url).scheme}://{urlsplit(response.url).netloc}"
        for path in COMMON_FEED_PATHS:
            feed_url = urljoin(origin, path)
            try:
                safe_url = await self._guard.validate(feed_url)
                feed_response = await self._fetcher.fetch(safe_url)
            except (FetchError, SourceUrlSecurityError):
                continue
            if _is_feed(feed_response):
                return _feed_result(
                    await self._guard.validate(feed_response.url), tested_at, direct=False
                )

        return _html_result(soup, response.url, tested_at)


class SourcePreviewService:
    def __init__(
        self,
        registry: CollectorRegistry,
        fetcher: Fetcher,
        classifier: Classifier,
        admission_policy: ContentAdmissionPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._fetcher = fetcher
        self._classifier = classifier
        self._admission_policy = admission_policy or ContentAdmissionPolicy()

    async def preview(self, discovery: DiscoveryResult) -> PreviewResult:
        if not discovery.usable:
            return PreviewResult((), ("当前检测结果没有可用于预览的通用采集器。",))
        source = Source(
            name="临时预览来源",
            source_type=discovery.source_type,
            start_url=discovery.normalized_url,
            enabled=False,
            default_category=None,
            collector_name=discovery.collector_name,
            collector_config=discovery.collector_config,
            origin=SourceOrigin.USER_ADDED,
            source_kind=(
                SourceKind.FALLBACK
                if discovery.source_type is SourceType.GITHUB_RELEASE
                else SourceKind.TEST
            ),
            source_tier=SourceTier.FALLBACK,
            audience=SourceAudience.ALL,
            homepage_visible=False,
            export_visible=False,
            content_scope=[],
            include_terms=[],
            exclude_terms=[],
            minimum_quality_score=15,
            accept_title_only=True,
            allow_external_links=False,
            allow_technical_updates=False,
        )
        collector = self._registry.create(source, self._fetcher)
        config = _preview_config(discovery)
        try:
            collected = await collector.collect(
                CollectContext(
                    source_url=discovery.normalized_url,
                    source_name=source.name,
                    config=config,
                )
            )
        except Exception:
            return PreviewResult((), ("抓取预览失败, 请检查来源状态后重试。",))

        items: list[PreviewItem] = []
        errors: list[str] = []
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()
        rejected = 0
        admission_reasons: dict[str, int] = {}
        for item in collected:
            if len(items) >= 10:
                break
            try:
                normalized_item = normalize_collected_item(item)
            except ItemNormalizationError:
                errors.append("已跳过一条无法规范化的预览记录。")
                rejected += 1
                continue
            admission = self._admission_policy.admit(normalized_item, source)
            if not admission.accepted:
                rejected += 1
                admission_reasons[admission.reason] = admission_reasons.get(admission.reason, 0) + 1
                continue
            normalized_url = normalized_item.canonical_url
            normalized_title = normalized_item.title
            title_key = normalized_title.casefold()
            if not normalized_title or normalized_url in seen_urls or title_key in seen_titles:
                rejected += 1
                continue
            try:
                classification = await self._classifier.classify(
                    normalized_item, source_default=None
                )
                category = classification.category
            except Exception:
                category = Category.UNCLASSIFIED
                errors.append("一条预览记录分类失败, 已标记为待分类。")
            items.append(
                PreviewItem(
                    title=_bounded_text(normalized_title, MAX_PREVIEW_TITLE_LENGTH) or "无标题",
                    url=normalized_url,
                    published_at=normalized_item.published_at,
                    summary=_bounded_text(normalized_item.summary, MAX_PREVIEW_SUMMARY_LENGTH),
                    category=category,
                )
            )
            seen_urls.add(normalized_url)
            seen_titles.add(title_key)
        if admission_reasons:
            summary = ", ".join(
                f"{reason}={count}" for reason, count in sorted(admission_reasons.items())
            )
            errors.append(f"内容准入已拒绝 {sum(admission_reasons.values())} 条: {summary}")
        if rejected >= 3 and rejected * 3 >= len(collected) * 2:
            errors.append("预览中重复或无效记录过多, 当前结果需要人工处理且不能直接启用。")
            items.clear()
        if not items:
            errors.append("没有抓取到有效的“标题 + 链接”, 当前来源不能直接启用。")
        return PreviewResult(tuple(items), tuple(dict.fromkeys(errors)))


def _github_result(url: str, tested_at: datetime) -> DiscoveryResult | None:
    parts = urlsplit(url)
    if (parts.hostname or "").casefold() not in {"github.com", "www.github.com"}:
        return None
    segments = [part for part in parts.path.split("/") if part]
    owner = segments[0] if segments else ""
    repository = segments[1].removesuffix(".git") if len(segments) >= 2 else ""
    valid = (
        len(segments) in {2, 3}
        and (len(segments) == 2 or segments[2].casefold() == "releases")
        and not parts.query
        and parts.path
        in {
            f"/{'/'.join(segments[:2])}",
            f"/{'/'.join(segments[:2])}/{segments[2]}" if len(segments) == 3 else "",
        }
        and _valid_github_owner(owner)
        and _valid_github_repository(repository)
    )
    if not valid:
        return _failed_result(
            url,
            DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR,
            "GitHub 地址必须是明确的 owner/repository 或其 Releases 页面。",
            tested_at,
        )
    normalized = f"https://github.com/{owner}/{repository}/releases"
    return DiscoveryResult(
        collector_name="github_release",
        source_type=SourceType.GITHUB_RELEASE,
        normalized_url=normalized,
        discovery_status=DiscoveryStatus.READY,
        discovery_confidence=0.99,
        requires_custom_collector=False,
        collector_config={"max_releases": 30, "include_prereleases": False},
        explanations=("已确认公开 GitHub 仓库格式, 将通过 Releases 采集器预览。",),
        errors=(),
        tested_at=tested_at,
    )


def _is_feed(response: FetchResult) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    parsed = cast(
        Mapping[str, object],
        feedparser.parse(response.content),  # pyright: ignore[reportUnknownMemberType]
    )
    version = str(parsed.get("version", "")).casefold()
    entries_value = parsed.get("entries", ())
    entries = cast(list[object], entries_value) if isinstance(entries_value, list) else []
    valid_entries = sum(_valid_feed_entry(entry, response.url) for entry in entries)
    has_reliable_entries = valid_entries > 0 and valid_entries * 2 >= len(entries)
    return (
        "rss" in content_type or "atom" in content_type or bool(version)
    ) and has_reliable_entries


def _looks_like_html(response: FetchResult) -> bool:
    content_type = response.headers.get("content-type", "").casefold()
    prefix = response.content[:1000].lstrip().lower()
    return "html" in content_type or b"<html" in prefix or b"<!doctype html" in prefix


def _valid_feed_entry(value: object, base_url: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    raw = cast(Mapping[object, object], value)
    entry = {str(key): child for key, child in raw.items()}
    title = entry.get("title")
    link = entry.get("link")
    return bool(
        isinstance(title, str)
        and title.strip()
        and isinstance(link, str)
        and canonicalize_url(link, base_url=base_url) is not None
    )


def _alternate_feed_urls(soup: BeautifulSoup, page_url: str) -> tuple[str, ...]:
    urls: list[str] = []
    for node in soup.select('link[rel~="alternate"][href]'):
        content_type = str(node.get("type", "")).casefold()
        href = node.get("href")
        if content_type not in {"application/rss+xml", "application/atom+xml"}:
            continue
        if isinstance(href, str):
            normalized = canonicalize_url(href, base_url=page_url)
            if normalized and normalized not in urls:
                urls.append(normalized)
        if len(urls) >= 4:
            break
    return tuple(urls)


def _feed_result(url: str, tested_at: datetime, *, direct: bool) -> DiscoveryResult:
    explanation = (
        "输入网址本身是有效的 RSS/Atom。"
        if direct
        else "从页面声明或有限的常见路径中确认了 RSS/Atom。"
    )
    return DiscoveryResult(
        collector_name="rss",
        source_type=SourceType.RSS,
        normalized_url=url,
        discovery_status=DiscoveryStatus.READY,
        discovery_confidence=0.98 if direct else 0.92,
        requires_custom_collector=False,
        collector_config={"max_items": 1000},
        explanations=(explanation,),
        errors=(),
        tested_at=tested_at,
    )


def _html_result(soup: BeautifulSoup, page_url: str, tested_at: datetime) -> DiscoveryResult:
    hostname = (urlsplit(page_url).hostname or "").casefold()
    for selector in HTML_SELECTOR_CANDIDATES:
        candidates = [_node_link(node, page_url, hostname) for node in soup.select(selector)]
        valid = {item for item in candidates if item is not None}
        unique_urls = {item[1] for item in valid}
        unique_titles = {item[0].casefold() for item in valid}
        count = min(len(unique_urls), len(unique_titles))
        if count >= 2:
            return DiscoveryResult(
                collector_name="html_list",
                source_type=SourceType.HTML_LIST,
                normalized_url=canonicalize_url(page_url) or page_url,
                discovery_status=DiscoveryStatus.READY if count >= 4 else DiscoveryStatus.PARTIAL,
                discovery_confidence=min(0.9, 0.68 + count * 0.04),
                requires_custom_collector=False,
                collector_config={
                    "allowed_domains": [hostname],
                    "allow_subdomains": False,
                    "discovery": {"mode": "selectors", "max_pages": 1, "max_depth": 0},
                    "extraction": {
                        "item_selector": selector,
                        "title_selector": "a[href]",
                        "link_selector": "a[href]",
                    },
                },
                explanations=(
                    f"在固定、可审计的常见列表结构中发现 {count} 个候选标题链接。",
                    "建议首次保存前人工检查标题、日期和简介的完整性。",
                ),
                errors=(),
                tested_at=tested_at,
            )

    candidates = {
        item
        for anchor in soup.select("a[href]")
        if (item := _anchor_link(anchor, page_url, hostname))
    }
    paths = list({urlsplit(item[1]).path for item in candidates})
    prefixes = [
        f"/{parts[0]}/" for path in paths if (parts := path.strip("/").split("/")) and parts[0]
    ]
    common = Counter(prefixes).most_common(1)
    if len(paths) >= 3 and common and common[0][1] >= 3 and common[0][1] / len(paths) >= 0.6:
        prefix = common[0][0]
        return DiscoveryResult(
            collector_name="html_list",
            source_type=SourceType.HTML_LIST,
            normalized_url=canonicalize_url(page_url) or page_url,
            discovery_status=DiscoveryStatus.PARTIAL,
            discovery_confidence=0.66,
            requires_custom_collector=False,
            collector_config={
                "allowed_domains": [hostname],
                "allow_subdomains": False,
                "discovery": {
                    "mode": "link_filter",
                    "max_pages": 1,
                    "max_depth": 0,
                    "include_url_patterns": [prefix],
                },
            },
            explanations=(
                f"发现多个共享 {prefix} 路径的站内标题链接, 使用有限链接过滤规则。",
                "页面结构未提供稳定日期或简介规则, 建议人工检查预览。",
            ),
            errors=(),
            tested_at=tested_at,
        )
    return _failed_result(
        canonicalize_url(page_url) or page_url,
        DiscoveryStatus.NEEDS_CUSTOM_COLLECTOR,
        "没有找到足够稳定的文章列表结构, 需要开发者提供自定义采集器。",
        tested_at,
    )


def _node_link(node: Tag, page_url: str, hostname: str) -> tuple[str, str] | None:
    anchor = node if node.name == "a" else node.select_one("a[href]")
    return _anchor_link(anchor, page_url, hostname) if isinstance(anchor, Tag) else None


def _anchor_link(anchor: Tag, page_url: str, hostname: str) -> tuple[str, str] | None:
    href = anchor.get("href")
    title = " ".join(anchor.get_text(" ", strip=True).split())
    if not isinstance(href, str) or len(title) < 4:
        return None
    if _in_non_content_region(anchor):
        return None
    url = canonicalize_url(href, base_url=page_url)
    if url is None or (urlsplit(url).hostname or "").casefold() != hostname:
        return None
    lowered = title.casefold()
    path = urlsplit(url).path.casefold()
    if any(
        term in lowered
        for term in (
            "登录",
            "注册",
            "联系我们",
            "关于我们",
            "友情链接",
            "login",
            "register",
            "contact",
            "privacy",
        )
    ) or any(
        segment in path
        for segment in ("/login", "/register", "/about", "/contact", "/privacy", "/links")
    ):
        return None
    return title, url


def _valid_github_owner(value: str) -> bool:
    return bool(
        1 <= len(value) <= 39
        and value.casefold() not in GITHUB_RESERVED_OWNERS
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", value)
        and "--" not in value
    )


def _in_non_content_region(anchor: Tag) -> bool:
    for node in (anchor, *anchor.parents):
        if node.name in {"nav", "footer", "header", "aside"}:
            return True
        class_value = node.get("class")
        classes = cast(list[object], class_value) if isinstance(class_value, list) else []
        identifiers = " ".join(
            [str(node.get("id", "")), *[str(value) for value in classes]]
        ).casefold()
        if any(term in identifiers for term in ("footer", "header", "sidebar", "nav", "menu")):
            return True
    return False


def _valid_github_repository(value: str) -> bool:
    return bool(
        1 <= len(value) <= 100 and GITHUB_PART_PATTERN.fullmatch(value) and value not in {".", ".."}
    )


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= limit else f"{cleaned[: limit - 1].rstrip()}…"


def _failed_result(
    url: str,
    status: DiscoveryStatus,
    message: str,
    tested_at: datetime,
) -> DiscoveryResult:
    return DiscoveryResult(
        collector_name="custom",
        source_type=SourceType.CUSTOM,
        normalized_url=url,
        discovery_status=status,
        discovery_confidence=0.0,
        requires_custom_collector=True,
        collector_config={},
        explanations=(message,),
        errors=(message,),
        tested_at=tested_at,
    )


def _preview_config(discovery: DiscoveryResult) -> dict[str, object]:
    config = cast(dict[str, object], dict(discovery.collector_config))
    if discovery.source_type is SourceType.RSS:
        config["max_items"] = 10
    elif discovery.source_type is SourceType.GITHUB_RELEASE:
        config["max_releases"] = 10
    elif discovery.source_type is SourceType.HTML_LIST:
        discovery_config = dict(cast(Mapping[str, object], config.get("discovery", {})))
        discovery_config["max_items"] = 10
        discovery_config["max_pages"] = 1
        discovery_config["max_depth"] = 0
        config["discovery"] = discovery_config
    return config


__all__ = [
    "DiscoveryTokenError",
    "DiscoveryTokenStore",
    "SourceDiscoveryService",
    "SourcePreviewService",
]
