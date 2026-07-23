"""Opt-in live source-onboarding checks; never use the configured database."""

import pytest

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.enums import SourceType
from app.domain.onboarding import DiscoveryResult, PreviewResult
from app.services.source_discovery import SourceDiscoveryService, SourcePreviewService
from app.services.source_url_security import SafeHttpFetcher, SourceUrlGuard

pytestmark = pytest.mark.network


async def _discover_and_preview(url: str) -> tuple[DiscoveryResult, PreviewResult]:
    guard = SourceUrlGuard()
    async with SafeHttpFetcher(guard, timeout_seconds=30) as fetcher:
        discovery = await SourceDiscoveryService(fetcher, guard).discover(url)
        preview = await SourcePreviewService(
            default_collector_registry(), fetcher, RuleBasedClassifier.from_yaml()
        ).preview(discovery)
    return discovery, preview


@pytest.mark.asyncio
async def test_django_weblog_rss_onboarding_live() -> None:
    discovery, preview = await _discover_and_preview("https://www.djangoproject.com/rss/weblog/")

    assert discovery.source_type is SourceType.RSS
    assert preview.items
    print(f"onboarding RSS previewed {len(preview.items)} items")


@pytest.mark.asyncio
async def test_deepseek_changelog_onboarding_is_bounded() -> None:
    discovery, preview = await _discover_and_preview("https://api-docs.deepseek.com/zh-cn/updates/")

    assert discovery.source_type in {SourceType.HTML_LIST, SourceType.CUSTOM}
    assert len(preview.items) <= 10


@pytest.mark.asyncio
async def test_sqlite_news_html_onboarding_live() -> None:
    discovery, preview = await _discover_and_preview("https://www.sqlite.org/news.html")

    assert discovery.source_type is SourceType.HTML_LIST
    assert preview.items
    print(f"onboarding HTML list previewed {len(preview.items)} items")
