"""Collector for public GitHub Releases API responses."""

import json
import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import quote, urlsplit

from app.collectors.rss import RSSCollector
from app.domain.collection import CollectContext, CollectedItem, Fetcher
from app.fetchers.errors import RateLimitFetchError
from app.utils.dates import parse_datetime
from app.utils.url import canonicalize_url


class GitHubReleaseCollector:
    """Collect release metadata without requiring a token or downloading assets."""

    name = "github_release"

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        owner, repository = _parse_repository(context.source_url)
        max_releases = max(1, min(_integer(context.config.get("max_releases"), 30), 100))
        include_prereleases = _boolean(context.config.get("include_prereleases"), False)
        api_url = (
            f"https://api.github.com/repos/{quote(owner)}/{quote(repository)}/releases"
            f"?per_page={max_releases}"
        )
        try:
            response = await self._fetcher.fetch(
                api_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
        except RateLimitFetchError:
            return await self._collect_atom_fallback(
                owner,
                repository,
                max_releases=max_releases,
                include_prereleases=include_prereleases,
            )
        payload = cast(object, json.loads(response.text))
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise ValueError("GitHub Releases API returned a non-list payload")

        items: list[CollectedItem] = []
        releases = cast(Sequence[object], payload)
        for raw_release in releases:
            try:
                if not isinstance(raw_release, Mapping):
                    continue
                release_mapping = cast(Mapping[object, object], raw_release)
                release: dict[str, object] = {
                    str(key): value for key, value in release_mapping.items()
                }
                if _boolean(release.get("draft"), False):
                    continue
                prerelease = _boolean(release.get("prerelease"), False)
                if prerelease and not include_prereleases:
                    continue
                tag_name = _text(release.get("tag_name"))
                title = _text(release.get("name")) or tag_name
                html_url = _text(release.get("html_url"))
                if not title or not html_url:
                    continue
                canonical_url = canonicalize_url(html_url)
                if canonical_url is None:
                    continue
                summary = _release_summary(
                    _text(release.get("body")),
                    _integer(context.config.get("summary_max_chars"), 500),
                )
                items.append(
                    CollectedItem(
                        title=title,
                        original_url=canonical_url,
                        canonical_url=canonical_url,
                        published_at=parse_datetime(
                            _text(release.get("published_at")) or _text(release.get("created_at"))
                        ),
                        summary=summary,
                        extra={"tag_name": tag_name or "", "prerelease": prerelease},
                    )
                )
            except (TypeError, ValueError):
                continue
        return items

    async def _collect_atom_fallback(
        self,
        owner: str,
        repository: str,
        *,
        max_releases: int,
        include_prereleases: bool,
    ) -> list[CollectedItem]:
        """Use GitHub's public Atom feed only when the preferred API quota is exhausted."""

        feed_url = f"https://github.com/{quote(owner)}/{quote(repository)}/releases.atom"
        feed_items = await RSSCollector(self._fetcher).collect(
            CollectContext(source_url=feed_url, config={"max_items": max_releases})
        )
        items: list[CollectedItem] = []
        for item in feed_items:
            prerelease = _looks_like_prerelease(item.title)
            if prerelease and not include_prereleases:
                continue
            items.append(
                CollectedItem(
                    title=item.title,
                    original_url=item.original_url,
                    canonical_url=item.canonical_url,
                    published_at=item.published_at,
                    summary=item.summary,
                    extra={
                        "api_fallback": "atom_rate_limit",
                        "prerelease": prerelease,
                        "tag_name": item.title,
                    },
                )
            )
            if len(items) >= max_releases:
                break
        return items


def _parse_repository(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if "://" not in candidate:
        parts = candidate.strip("/").split("/")
    else:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if hostname == "api.github.com" and len(path_parts) >= 3 and path_parts[0] == "repos":
            parts = path_parts[1:3]
        elif hostname in {"github.com", "www.github.com"}:
            parts = path_parts[:2]
        else:
            raise ValueError("GitHub source must use github.com or api.github.com")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError("GitHub source must identify an owner and repository")
    return parts[0], parts[1].removesuffix(".git")


def _release_summary(body: str | None, max_chars: int) -> str | None:
    if not body:
        return None
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = re.sub(r"!?(?:\[([^]]*)\])\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*_`~-]+", " ", text)
    text = " ".join(text.split())
    limit = max(50, min(max_chars, 5000))
    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def _looks_like_prerelease(title: str) -> bool:
    return bool(
        re.search(r"(?:^|[.\-_ ])(?:alpha|beta|preview|pre|rc)\d*(?:$|[.\-_ ])", title, re.I)
    )


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _boolean(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default
