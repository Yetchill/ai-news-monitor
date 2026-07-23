"""Opt-in live checks for a bounded subset of stage-eight formal sources."""

import pytest

from app.collectors.html_list import HTMLListCollector
from app.domain.collection import CollectContext, CollectedItem
from app.fetchers.http import HttpFetcher

pytestmark = pytest.mark.network


def _assert_valid(items: list[CollectedItem]) -> None:
    assert items
    assert all(item.title.strip() for item in items)
    assert all(item.canonical_url.startswith(("http://", "https://")) for item in items)
    assert all("login" not in item.canonical_url.casefold() for item in items)


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "config"),
    [
        (
            "https://www.nda.gov.cn/sjj/swdt/list/index_pc_1.html",
            {
                "allowed_domains": ["www.nda.gov.cn"],
                "discovery": {"mode": "selectors", "max_pages": 1, "max_depth": 0},
                "extraction": {
                    "item_selector": ".u-list > li",
                    "title_selector": "a",
                    "link_selector": "a",
                    "date_selector": "span",
                },
            },
        ),
        (
            "https://www.cac.gov.cn/wxzw/zcfg/A093703index_1.htm",
            {
                "allowed_domains": ["www.cac.gov.cn"],
                "discovery": {"mode": "selectors", "max_pages": 1, "max_depth": 0},
                "extraction": {
                    "item_selector": "#loadingInfoPage > li",
                    "title_selector": "h5 a",
                    "link_selector": "h5 a",
                    "date_selector": ".times",
                },
            },
        ),
        (
            "https://www.isc.org.cn/category/7330.html",
            {
                "allowed_domains": ["www.isc.org.cn"],
                "discovery": {"mode": "selectors", "max_pages": 1, "max_depth": 0},
                "extraction": {
                    "item_selector": ".news-list > li",
                    "title_selector": "h3",
                    "link_selector": "a",
                    "date_selector": ".msg span",
                },
            },
        ),
        (
            "https://cloud.baidu.com/news/news",
            {
                "allowed_domains": ["cloud.baidu.com"],
                "discovery": {
                    "mode": "link_filter",
                    "max_pages": 1,
                    "max_depth": 0,
                    "include_url_patterns": ["/news/news_"],
                },
            },
        ),
    ],
)
async def test_official_html_lists_live(url: str, config: dict[str, object]) -> None:
    async with HttpFetcher(timeout_seconds=30, request_interval_seconds=0) as fetcher:
        items = await HTMLListCollector(fetcher).collect(
            CollectContext(source_url=url, config=config)
        )

    _assert_valid(items)
    print(f"official HTML source {url} collected {len(items)} items")
