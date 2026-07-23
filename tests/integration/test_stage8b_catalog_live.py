"""Bounded, read-only accessibility probes for every stage-eight-B candidate."""

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from app.domain.enums import LifecycleState
from app.services.source_catalog_service import SourceCatalogEntry, load_source_catalog
from app.services.source_url_security import SafeHttpFetcher, SourceUrlGuard

pytestmark = pytest.mark.network


@dataclass(frozen=True, slots=True)
class Probe:
    slug: str
    status: int | None
    final_url: str | None
    title_visible: bool
    date_visible: bool
    detail_link_visible: bool
    javascript_likely: bool
    login_or_captcha: bool
    error: str | None


async def _probe(entry: SourceCatalogEntry, semaphore: asyncio.Semaphore) -> Probe:
    async with semaphore:
        try:
            async with SafeHttpFetcher(SourceUrlGuard(), timeout_seconds=15) as fetcher:
                response = await fetcher.fetch(entry.url)
            text = response.text[:2_000_000]
            soup = BeautifulSoup(text, "lxml")
            visible = " ".join(soup.stripped_strings)
            links = [
                href
                for node in soup.select("a[href]")[:500]
                if isinstance((href := node.get("href")), str)
            ]
            host = (urlsplit(response.url).hostname or "").casefold()
            detail = any(
                href.startswith(("http://", "https://", "/"))
                and (not href.startswith("http") or host in href.casefold())
                for href in links
            )
            scripts = len(soup.select("script"))
            return Probe(
                entry.slug,
                response.status_code,
                response.url,
                bool(soup.title and soup.title.get_text(strip=True)) or len(visible) > 200,
                bool(re.search(r"20\d{2}[-/.年]\d{1,2}", visible)),
                detail,
                len(visible) < 300 and scripts >= 3,
                any(term in visible.casefold() for term in ("验证码", "captcha", "请登录")),
                None,
            )
        except Exception as exc:  # Network diagnostics must report, not forge success.
            return Probe(entry.slug, None, None, False, False, False, False, False, str(exc)[:300])


@pytest.mark.asyncio
async def test_all_candidate_accessibility_and_technical_difficulty_live() -> None:
    entries = [
        entry
        for entry in load_source_catalog()
        if entry.lifecycle_state is LifecycleState.CANDIDATE
    ]
    semaphore = asyncio.Semaphore(4)
    results = await asyncio.gather(*(_probe(entry, semaphore) for entry in entries))

    assert len(results) == len(entries) == 8
    assert {result.slug for result in results} == {entry.slug for entry in entries}
    for result in results:
        print(result)
