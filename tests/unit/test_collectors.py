"""Offline collector tests backed by fixed RSS, HTML, and JSON samples."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.collectors.cls_topic import CLSTopicCollector
from app.collectors.github_release import GitHubReleaseCollector
from app.collectors.html_list import HTMLListCollector
from app.collectors.huxiu import HuxiuCollector
from app.collectors.infoq import InfoQAICollector
from app.collectors.minimax_news import MiniMaxNewsCollector
from app.collectors.public_json import PublicJsonCollector
from app.collectors.registry import CollectorRegistry, default_collector_registry
from app.collectors.rss import RSSCollector
from app.collectors.single_page_changelog import SinglePageChangelogCollector
from app.domain.collection import CollectContext, FetchResult
from app.domain.enums import SourceOrigin, SourceType
from app.domain.models import Source
from app.fetchers.errors import ForbiddenFetchError, RateLimitFetchError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FixtureFetcher:
    def __init__(self, responses: Mapping[str, bytes]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, str] | None]] = []
        self.post_requests: list[tuple[str, str, Mapping[str, str] | None]] = []

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        self.requests.append((url, headers))
        return FetchResult(
            requested_url=url,
            url=url,
            status_code=200,
            headers={},
            content=self.responses[url],
        )

    async def post(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        self.post_requests.append((url, body, headers))
        key = f"{url}|{body}"
        return FetchResult(
            requested_url=url,
            url=url,
            status_code=200,
            headers={},
            content=self.responses[key],
        )


class RateLimitedGitHubFetcher(FixtureFetcher):
    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        if url.startswith("https://api.github.com/"):
            self.requests.append((url, headers))
            raise RateLimitFetchError(url, "quota exhausted")
        return await super().fetch(url, headers=headers)


class ForbiddenGitHubFetcher(FixtureFetcher):
    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        self.requests.append((url, headers))
        raise ForbiddenFetchError(url, "permission denied")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected_title"),
    [("sample_rss.xml", "Example model"), ("sample_atom.xml", "Atom release")],
)
async def test_rss_collector_supports_rss_and_atom(
    fixture_name: str,
    expected_title: str,
) -> None:
    url = "https://example.com/feed.xml"
    fetcher = FixtureFetcher({url: (FIXTURES / fixture_name).read_bytes()})

    items = await RSSCollector(fetcher).collect(CollectContext(source_url=url))

    assert items
    assert expected_title in items[0].title
    assert items[0].canonical_url.startswith("https://example.com/")
    assert items[0].published_at is not None
    assert items[0].summary
    if fixture_name == "sample_rss.xml":
        assert len(items) == 2
        assert items[0].canonical_url == "https://example.com/news/model-v1"


@pytest.mark.asyncio
async def test_rss_collector_enforces_configured_result_limit() -> None:
    url = "https://example.com/feed.xml"
    fetcher = FixtureFetcher({url: (FIXTURES / "sample_rss.xml").read_bytes()})

    items = await RSSCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 1})
    )

    assert len(items) == 1


@pytest.mark.asyncio
async def test_qbitai_official_feed_preserves_dates_links_and_deduplicates() -> None:
    url = "https://www.qbitai.com/category/%E8%B5%84%E8%AE%AF/feed"
    fetcher = FixtureFetcher({url: (FIXTURES / "qbitai_feed.xml").read_bytes()})

    items = await RSSCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert [item.title for item in items] == [
        "国产大模型发布重要能力升级",
        "Agent 平台开放全新 API",
    ]
    assert [
        item.published_at.date().isoformat() if item.published_at else None for item in items
    ] == [
        "2026-07-20",
        "2026-07-19",
    ]
    assert [item.canonical_url for item in items] == [
        "https://www.qbitai.com/2026/07/310001.html",
        "https://www.qbitai.com/2026/07/310002.html",
    ]


@pytest.mark.asyncio
async def test_cls_topic_uses_public_embedded_json_with_dates_and_stable_links() -> None:
    url = "https://www.cls.cn/subject/1321"
    fetcher = FixtureFetcher({url: (FIXTURES / "cls_topic.html").read_bytes()})

    items = await CLSTopicCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert [item.title for item in items] == [
        "人工智能产业发布重要模型更新",
        "智能体平台进入行业应用阶段",
    ]
    assert [item.canonical_url for item in items] == [
        "https://www.cls.cn/detail/2430001",
        "https://www.cls.cn/detail/2430002",
    ]
    assert all(item.published_at is not None for item in items)
    assert items[0].extra == {
        "public_payload": "__NEXT_DATA__",
        "article_id": "2430001",
    }
    assert items[0].summary == "公开专题摘要。"
    assert items[1].summary == (
        "【智能体平台进入行业应用阶段】财联社7月19日电\uff0c据称该平台将继续升级。"
    )


@pytest.mark.asyncio
async def test_deepseek_changelog_pairs_date_heading_with_update_heading() -> None:
    url = "https://api-docs.deepseek.com/zh-cn/updates/"
    fetcher = FixtureFetcher({url: (FIXTURES / "deepseek_changelog.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": ".theme-doc-markdown",
                "date_heading_selector": "h2",
                "entry_selector": "h3",
                "append_summary_when_title_lacks_action": True,
            },
        )
    )

    assert [
        item.published_at.date().isoformat() if item.published_at else None for item in items
    ] == [
        "2026-04-24",
        "2025-12-01",
    ]
    assert items[0].title.startswith("DeepSeek-V4\uff1a")
    assert "正式发布" in items[0].title
    assert items[0].canonical_url != items[1].canonical_url


@pytest.mark.asyncio
async def test_kimi_changelog_pairs_each_update_with_section_date_and_filters_noise() -> None:
    url = "https://platform.kimi.com/blog/posts/changelog"
    fetcher = FixtureFetcher({url: (FIXTURES / "kimi_changelog.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": "article",
                "date_heading_selector": "h2",
                "entry_selector": "li",
                "include_entry_terms": ["模型", "API"],
            },
        )
    )

    assert [item.title for item in items] == [
        "Kimi K2 Think 模型及其 turbo 版本模型正式发布",
        "kimi-k2-0905-preview 模型上线发布",
    ]
    assert items[0].published_at is not None
    assert items[0].published_at.date().isoformat() == "2025-11-06"
    assert items[0].extra["original_time_text"] == "2025年11月6日"


@pytest.mark.asyncio
async def test_qianfan_changelog_builds_stable_model_action_rows_with_inherited_year() -> None:
    url = "https://cloud.baidu.com/doc/qianfan/s/Kmh4stnjp"
    fetcher = FixtureFetcher({url: (FIXTURES / "qianfan_changelog.html").read_bytes()})

    context = CollectContext(
        source_url=url,
        config={
            "content_selector": ".post__body",
            "date_heading_selector": "h2",
            "entry_selector": "tbody tr",
            "entry_title_cells": [2, 5],
            "entry_date_cell": 0,
            "entry_summary_cell": 6,
            "entry_title_replacements": {"上新": "正式发布"},
        },
    )
    first = await SinglePageChangelogCollector(fetcher).collect(context)
    second = await SinglePageChangelogCollector(fetcher).collect(context)

    assert [item.title for item in first] == [
        "Kimi-K2.5 — 退役",
        "Example-Model — 正式发布",
    ]
    assert first[0].published_at is not None
    assert first[0].published_at.date().isoformat() == "2026-07-09"
    assert [item.canonical_url for item in first] == [item.canonical_url for item in second]


@pytest.mark.asyncio
async def test_zhipu_research_uses_public_embedded_ids_for_stable_detail_links() -> None:
    url = "https://www.zhipuai.cn/zh/research"
    fetcher = FixtureFetcher({url: (FIXTURES / "zhipu_research.html").read_bytes()})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["www.zhipuai.cn"],
                "discovery": {"mode": "selectors", "max_items": 20},
                "extraction": {
                    "item_selector": "main div.border-b",
                    "title_selector": "div.text-xl",
                    "date_selector": "p",
                    "embedded_title_key": "title_zh",
                    "embedded_link_key": "id",
                    "embedded_link_template": "/zh/research/{value}",
                },
            },
        )
    )

    assert [item.canonical_url for item in items] == [
        "https://www.zhipuai.cn/zh/research/161",
        "https://www.zhipuai.cn/zh/research/156",
    ]
    assert all(item.published_at is not None for item in items)


@pytest.mark.asyncio
async def test_media_selector_parses_relative_time_and_filters_financing_stock_and_ipo() -> None:
    url = "https://36kr.com/newsflashes"
    fetcher = FixtureFetcher({url: (FIXTURES / "media_relative_list.html").read_bytes()})
    fixed_now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    items = await HTMLListCollector(fetcher, clock=lambda: fixed_now).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["36kr.com"],
                "filter_selector_items": True,
                "discovery": {
                    "mode": "selectors",
                    "include_text": ["AI", "大模型"],
                    "exclude_text": ["融资", "股票", "IPO", "上市"],
                },
                "extraction": {
                    "item_selector": ".newsflash-item",
                    "title_selector": "a.item-title",
                    "link_selector": "a.item-title",
                    "date_selector": ".time",
                },
            },
        )
    )

    assert [item.title for item in items] == ["某公司发布新一代 AI 大模型"]
    assert items[0].published_at == datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    assert items[0].extra["original_time_text"] == "2小时前"


@pytest.mark.asyncio
async def test_media_selector_uses_nearest_group_date_when_item_time_is_empty() -> None:
    url = "https://zhidx.com/news"
    fetcher = FixtureFetcher({url: (FIXTURES / "media_relative_list.html").read_bytes()})
    fixed_now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)

    items = await HTMLListCollector(fetcher, clock=lambda: fixed_now).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["zhidx.com"],
                "discovery": {"mode": "selectors"},
                "extraction": {
                    "item_selector": ".news-item",
                    "title_selector": "a.title",
                    "link_selector": "a.title",
                    "date_selector": ".time",
                    "ancestor_date_selector": ".title-name",
                },
            },
        )
    )

    assert [item.title for item in items] == ["智能体平台发布重要升级"]
    assert items[0].published_at == datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    assert items[0].extra["original_time_text"] == "昨天"


@pytest.mark.asyncio
async def test_html_selector_mode_extracts_metadata_and_bounded_pagination() -> None:
    first_url = "https://example.com/news"
    second_url = "https://example.com/news?page=2"
    fetcher = FixtureFetcher(
        {
            first_url: (FIXTURES / "sample_list.html").read_bytes(),
            second_url: (FIXTURES / "sample_list_page_2.html").read_bytes(),
        }
    )
    context = CollectContext(
        source_url=first_url,
        config={
            "allowed_domains": ["example.com"],
            "discovery": {
                "mode": "selectors",
                "max_pages": 2,
                "max_depth": 1,
                "pagination_selector": "a.next",
            },
            "extraction": {
                "item_selector": ".news-list li",
                "title_selector": ".title",
                "link_selector": "a",
                "date_selector": "time, .date",
                "summary_selector": ".summary",
            },
        },
    )

    items = await HTMLListCollector(fetcher).collect(context)

    assert [item.title for item in items] == [
        "人工智能行业标准正式发布",
        "优秀应用案例征集通知",
        "第二页行业动态",
    ]
    assert all(item.canonical_url.startswith("https://example.com/") for item in items)
    assert items[0].published_at is not None
    assert items[0].summary == "标准覆盖智能体产品的基础评测要求。"
    assert [request[0] for request in fetcher.requests] == [first_url, second_url]


@pytest.mark.asyncio
async def test_html_selector_mode_can_pair_visible_titles_with_embedded_links() -> None:
    url = "https://example.com/"
    html = b"""
        <div class="news"><h3>Visible server-rendered news title</h3><span>2026-07-17</span></div>
        <script>self.data = {\"title\":\"Visible server-rendered news title\",\"external_url\":\"https://example.com/article/1\"}</script>
    """
    fetcher = FixtureFetcher({url: html})
    context = CollectContext(
        source_url=url,
        config={
            "allowed_domains": ["example.com"],
            "discovery": {"mode": "selectors"},
            "extraction": {
                "item_selector": ".news",
                "title_selector": "h3",
                "date_selector": "span",
                "embedded_title_key": "title",
                "embedded_link_key": "external_url",
            },
        },
    )

    items = await HTMLListCollector(fetcher).collect(context)

    assert len(items) == 1
    assert items[0].canonical_url == "https://example.com/article/1"
    assert items[0].published_at is not None


@pytest.mark.asyncio
async def test_html_link_filter_rejects_navigation_assets_and_external_domains() -> None:
    url = "https://example.com/"
    fetcher = FixtureFetcher({url: (FIXTURES / "sample_home.html").read_bytes()})
    context = CollectContext(
        source_url=url,
        config={
            "allowed_domains": ["example.com"],
            "discovery": {
                "mode": "link_filter",
                "include_text": ["人工智能", "案例", "成果", "通知"],
                "exclude_url_patterns": ["/login", "/contact"],
            },
        },
    )

    items = await HTMLListCollector(fetcher).collect(context)

    assert [item.title for item in items] == [
        "联盟发布人工智能产业最新研究成果",
        "关于开展优秀人工智能案例征集的通知",
    ]
    assert all(item.published_at is not None for item in items)
    assert items[0].summary == "报告总结了产业应用的最新进展。"


@pytest.mark.asyncio
async def test_html_collector_bounds_results_and_isolates_malformed_links() -> None:
    url = "https://example.com/"
    html = b"""
        <a href="https://[malformed">Malformed link should be skipped</a>
        <a href="/article/1">First valid article title</a>
        <a href="/article/2">Second valid article title</a>
        <a href="/article/3">Third valid article title</a>
    """
    fetcher = FixtureFetcher({url: html})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["example.com"],
                "discovery": {"mode": "link_filter", "max_items": 2},
            },
        )
    )

    assert [item.title for item in items] == [
        "First valid article title",
        "Second valid article title",
    ]


@pytest.mark.asyncio
async def test_html_allowed_domains_accept_root_and_subdomain_but_not_forged_suffix() -> None:
    url = "https://aiiaorg.cn/"
    html = b"""
        <a href="https://aiiaorg.cn/article/1">Root domain article title</a>
        <a href="https://news.aiiaorg.cn/article/2">Subdomain article title</a>
        <a href="https://evil-aiiaorg.cn/article/3">Forged suffix article title</a>
    """
    fetcher = FixtureFetcher({url: html})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["aiiaorg.cn"],
                "allow_subdomains": True,
                "discovery": {"mode": "link_filter"},
            },
        )
    )

    assert [item.canonical_url for item in items] == [
        "https://aiiaorg.cn/article/1",
        "https://news.aiiaorg.cn/article/2",
    ]


@pytest.mark.asyncio
async def test_html_pagination_deduplicates_cycles_before_queueing() -> None:
    first_url = "https://example.com/news"
    second_url = "https://example.com/news?page=2"
    first_page = b"""
        <div class="item"><a href="/article/1">First page article</a></div>
        <a class="next" href="/news">Current page</a>
        <a class="next" href="/news?page=2">Next page</a>
        <a class="next" href="/news?page=2">Duplicate next page</a>
    """
    second_page = b"""
        <div class="item"><a href="/article/2">Second page article</a></div>
        <a class="next" href="/news">Back to first page</a>
    """
    fetcher = FixtureFetcher({first_url: first_page, second_url: second_page})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=first_url,
            config={
                "allowed_domains": ["example.com"],
                "discovery": {
                    "mode": "selectors",
                    "max_pages": 10,
                    "max_depth": 3,
                    "pagination_selector": "a.next",
                },
                "extraction": {"item_selector": ".item", "link_selector": "a"},
            },
        )
    )

    assert [item.title for item in items] == ["First page article", "Second page article"]
    assert [request[0] for request in fetcher.requests] == [first_url, second_url]


@pytest.mark.asyncio
async def test_github_collector_uses_public_api_and_ignores_assets_and_prereleases() -> None:
    api_url = "https://api.github.com/repos/QwenLM/Qwen-Agent/releases?per_page=30"
    fetcher = FixtureFetcher({api_url: (FIXTURES / "github_releases.json").read_bytes()})

    items = await GitHubReleaseCollector(fetcher).collect(
        CollectContext(source_url="https://github.com/QwenLM/Qwen-Agent/releases")
    )

    assert len(items) == 1
    assert items[0].title == "Qwen-Agent v0.1.0"
    assert items[0].published_at is not None
    assert items[0].summary == "Highlights Added tool support and fixes."
    assert "assets" not in items[0].extra
    assert fetcher.requests[0][1] is not None
    assert fetcher.requests[0][1]["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_github_collector_falls_back_to_public_atom_when_api_is_rate_limited() -> None:
    feed_url = "https://github.com/QwenLM/Qwen-Agent/releases.atom"
    fetcher = RateLimitedGitHubFetcher({feed_url: (FIXTURES / "sample_atom.xml").read_bytes()})

    items = await GitHubReleaseCollector(fetcher).collect(
        CollectContext(source_url="https://github.com/QwenLM/Qwen-Agent/releases")
    )

    assert len(items) == 1
    assert items[0].extra == {
        "api_fallback": "atom_rate_limit",
        "prerelease": False,
        "tag_name": "Atom release entry",
    }
    assert [request[0] for request in fetcher.requests] == [
        "https://api.github.com/repos/QwenLM/Qwen-Agent/releases?per_page=30",
        feed_url,
    ]


@pytest.mark.asyncio
async def test_github_collector_does_not_fallback_for_ordinary_forbidden_response() -> None:
    fetcher = ForbiddenGitHubFetcher({})

    with pytest.raises(ForbiddenFetchError):
        await GitHubReleaseCollector(fetcher).collect(
            CollectContext(source_url="https://github.com/QwenLM/Qwen-Agent/releases")
        )

    assert [request[0] for request in fetcher.requests] == [
        "https://api.github.com/repos/QwenLM/Qwen-Agent/releases?per_page=30"
    ]


def test_registry_constructs_by_collector_name_and_accepts_extensions() -> None:
    fetcher = FixtureFetcher({})
    source = Source(
        name="Example",
        source_type=SourceType.RSS,
        start_url="https://example.com/feed",
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
    )
    registry = default_collector_registry()

    assert isinstance(registry.create(source, fetcher), RSSCollector)
    assert registry.names() == (
        "case_hub",
        "cls_topic",
        "document_hub",
        "github_release",
        "html_list",
        "huxiu",
        "infoq_ai",
        "minimax_news",
        "public_json",
        "rss",
        "single_page_changelog",
    )

    custom_registry = CollectorRegistry()
    custom_registry.register("rss", RSSCollector)
    assert isinstance(custom_registry.create(source, fetcher), RSSCollector)


@pytest.mark.asyncio
async def test_hunyuan_product_updates_extracts_title_date_link_from_slate_table() -> None:
    url = "https://cloud.tencent.com/document/product/1729/97765"
    fetcher = FixtureFetcher({url: (FIXTURES / "hunyuan_product_updates.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": "div#doc-slate-root",
                "date_heading_selector": "h2",
                "entry_selector": "tbody tr",
                "entry_title_cells": [0],
                "entry_date_cell": 2,
                "entry_summary_cell": 1,
                "entry_link_cell": 3,
                "exclude_entry_terms": ["动态名称"],
            },
        )
    )

    assert [item.title for item in items] == [
        "Tencent HY 文生文旧版本模型下线",
        "Tencent HY Vision 1.5 Instruct 上线",
    ]
    assert items[0].published_at is not None
    assert items[0].published_at.date().isoformat() == "2026-06-22"  # type: ignore[union-attr]
    assert items[1].published_at is not None
    assert items[1].published_at.date().isoformat() == "2025-12-17"  # type: ignore[union-attr]
    assert items[0].canonical_url.startswith("https://cloud.tencent.com/document/product/1729/97765?entry=")
    assert items[1].canonical_url.startswith("https://cloud.tencent.com/document/product/1729/97765?entry=")
    assert items[0].canonical_url != items[1].canonical_url
    assert items[0].extra["detail_link"] == "/document/product/1729/131925"
    assert items[1].extra["detail_link"] == "https://cloud.tencent.com/document/product/1729/104753"
    assert items[0].summary is not None
    assert "模型" in items[0].summary
    assert items[1].summary is not None
    assert "TurboS" in items[1].summary


@pytest.mark.asyncio
async def test_hunyuan_product_announcements_extracts_title_date_link_from_slate_table() -> None:
    url = "https://cloud.tencent.com/document/product/1729/132069"
    fetcher = FixtureFetcher({url: (FIXTURES / "hunyuan_product_announcements.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": "div#doc-slate-root",
                "date_heading_selector": "h3",
                "entry_selector": "tbody tr",
                "entry_title_cells": [0],
                "entry_date_cell": 1,
                "entry_link_cell": 0,
                "exclude_entry_terms": ["公告标题"],
            },
        )
    )

    assert [item.title for item in items] == [
        "关于腾讯云混元多模态模型服务迁移通知",
        "关于腾讯云混元旧版本模型下线的通知",
    ]
    assert items[0].published_at is not None
    assert items[0].published_at.date().isoformat() == "2026-06-05"  # type: ignore[union-attr]
    assert items[1].published_at is not None
    assert items[1].published_at.date().isoformat() == "2026-05-22"  # type: ignore[union-attr]
    assert items[0].canonical_url.startswith("https://cloud.tencent.com/document/product/1729/132069?entry=")
    assert items[1].canonical_url.startswith("https://cloud.tencent.com/document/product/1729/132069?entry=")
    assert items[0].canonical_url != items[1].canonical_url
    assert items[0].extra["detail_link"] == "https://cloud.tencent.com/announce/detail/2310"
    assert items[1].extra["detail_link"] == "https://cloud.tencent.com/announce/detail/2301"


@pytest.mark.asyncio
async def test_hunyuan_product_updates_skips_header_rows() -> None:
    url = "https://cloud.tencent.com/document/product/1729/97765"
    fetcher = FixtureFetcher({url: (FIXTURES / "hunyuan_product_updates.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": "div#doc-slate-root",
                "date_heading_selector": "h2",
                "entry_selector": "tbody tr",
                "entry_title_cells": [0],
                "entry_date_cell": 2,
                "entry_summary_cell": 1,
                "entry_link_cell": 3,
                "exclude_entry_terms": ["动态名称", "公告标题"],
            },
        )
    )

    assert all("动态名称" not in item.title for item in items)
    assert all("动态描述" not in item.title for item in items)
    assert len(items) == 2


@pytest.mark.asyncio
async def test_hunyuan_product_announcements_works_without_date_headings() -> None:
    url = "https://cloud.tencent.com/document/product/1729/132069"
    fetcher = FixtureFetcher({url: (FIXTURES / "hunyuan_product_announcements.html").read_bytes()})

    items = await SinglePageChangelogCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "content_selector": "div#doc-slate-root",
                "date_heading_selector": "h3",
                "entry_selector": "tbody tr",
                "entry_title_cells": [0],
                "entry_date_cell": 1,
                "exclude_entry_terms": ["公告标题"],
            },
        )
    )

    assert len(items) == 2
    assert all(item.published_at is not None for item in items)
    assert all(item.title for item in items)


@pytest.mark.asyncio
async def test_minimax_news_parses_public_json_api_with_dates_and_links() -> None:
    url = "https://www.minimaxi.com/api/news"
    fetcher = FixtureFetcher({url: (FIXTURES / "minimax_news.json").read_bytes()})

    items = await MiniMaxNewsCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 50})
    )

    assert len(items) == 12
    assert all(item.published_at is not None for item in items)
    assert all(
        item.canonical_url.startswith("https://www.minimaxi.com/news/") for item in items
    )
    assert all(item.extra.get("public_payload") == "api/news" for item in items)
    assert items[0].title == "华为云与MiniMax最新模型M3完成适配"
    assert items[0].extra.get("slug") == "huawei-cloud-minimax-m3-adaptation-copy"
    assert items[0].published_at is not None
    assert items[0].published_at.date().isoformat() == "2026-06-16"
    assert items[0].summary is not None
    assert len(items[0].summary) > 20
    assert isinstance(items[0].extra.get("tags"), tuple)
    extra_tags = items[0].extra.get("tags", ())
    assert isinstance(extra_tags, tuple)
    assert len(extra_tags) >= 1  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_minimax_news_enforces_configured_result_limit() -> None:
    url = "https://www.minimaxi.com/api/news"
    fetcher = FixtureFetcher({url: (FIXTURES / "minimax_news.json").read_bytes()})

    items = await MiniMaxNewsCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 3})
    )

    assert len(items) == 3


@pytest.mark.asyncio
async def test_minimax_news_handles_invalid_json_gracefully() -> None:
    url = "https://www.minimaxi.com/api/news"
    fetcher = FixtureFetcher({url: b"not valid json"})

    items = await MiniMaxNewsCollector(fetcher).collect(
        CollectContext(source_url=url)
    )

    assert items == []


@pytest.mark.asyncio
async def test_xinhua_tech_filters_to_ai_content_only() -> None:
    url = "https://www.news.cn/tech/index.html"
    fetcher = FixtureFetcher({url: (FIXTURES / "xinhua_tech.html").read_bytes()})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["www.news.cn"],
                "filter_selector_items": True,
                "discovery": {
                    "mode": "selectors",
                    "max_pages": 1,
                    "max_depth": 0,
                    "max_items": 20,
                    "include_text": [
                        "人工智能", "大模型", "智能体", "AI",
                        "算力", "开源", "模型", "Agent", "算法",
                    ],
                    "exclude_text": [
                        "大会前瞻", "论坛开幕", "消费电子", "手机", "家电",
                        "汽车", "新能源", "火箭", "快递", "物流", "二手车",
                        "人物", "采访",
                    ],
                },
                "extraction": {
                    "item_selector": ".item",
                    "title_selector": ".tit a",
                    "link_selector": ".tit a",
                    "date_selector": ".time",
                },
            },
        )
    )

    titles = [item.title for item in items]
    assert len(items) >= 5
    assert "推动人工智能嵌入日常生活" in titles
    assert "AI迈入智能体时代 安全能力建设提速" in titles
    assert "中国企业发布全球最大规模的开源模型Kimi K3" in titles
    assert all(item.canonical_url.startswith("http") for item in items)
    assert all(item.published_at is not None for item in items)
    non_ai = [
        "上半年规模以上工业增加值同比增5.4%",
        "快递包装减量不等于防护降标",
        "破解新能源汽车维修难题",
        "二手车流通新生态加速成型",
        "中国可回收火箭开辟竞争新赛道",
    ]
    for na_title in non_ai:
        assert na_title not in titles


@pytest.mark.asyncio
async def test_public_json_collector_extracts_qwen_blog_articles_with_bounds() -> None:
    url = "https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=zh-CN"
    fetcher = FixtureFetcher({url: (FIXTURES / "qwen_blog.json").read_bytes()})

    items = await PublicJsonCollector(fetcher).collect(
        CollectContext(
            source_url="https://qwen.ai/api/v2/article/retrieval",
            config={
                "query_params": "type=qwen_ai&language=zh-CN",
                "max_items": 20,
                "items_field": "data.articles",
                "link_template": "https://qwen.ai/blog/{path}",
                "date_field": "date",
                "date_nested_in": "extra",
                "date_format": "iso",
            },
        )
    )

    assert len(items) == 4
    assert [item.title for item in items] == [
        "Qwen DeepResearch: 当灵感不再需要理由",
        "SAPO：一种稳定且高性能的大语言模型强化学习方法",  # noqa: RUF001
        "Qwen-Image-Edit-2511: 一致性再提升",
        "Qwen-Image-Layered: 面向内在可编辑性的图层分解",
    ]
    assert [item.canonical_url for item in items] == [
        "https://qwen.ai/blog/qwen-deepresearch",
        "https://qwen.ai/blog/sapo",
        "https://qwen.ai/blog/qwen-image-edit-2511",
        "https://qwen.ai/blog/qwen-image-layered",
    ]
    assert all(item.published_at is not None for item in items)
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2025
    assert items[0].published_at.month == 11
    assert items[0].published_at.day == 12
    assert items[0].extra == {"collector": "public_json"}


@pytest.mark.asyncio
async def test_public_json_collector_enforces_item_limit() -> None:
    url = "https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=zh-CN"
    fetcher = FixtureFetcher({url: (FIXTURES / "qwen_blog.json").read_bytes()})

    items = await PublicJsonCollector(fetcher).collect(
        CollectContext(
            source_url="https://qwen.ai/api/v2/article/retrieval",
            config={
                "query_params": "type=qwen_ai&language=zh-CN",
                "max_items": 2,
                "items_field": "data.articles",
                "link_template": "https://qwen.ai/blog/{path}",
                "date_field": "date",
                "date_nested_in": "extra",
                "date_format": "iso",
            },
        )
    )

    assert len(items) == 2


@pytest.mark.asyncio
async def test_public_json_collector_rejects_oversized_response() -> None:
    url = "https://qwen.ai/api/v2/article/retrieval?type=qwen_ai&language=zh-CN"
    raw = (FIXTURES / "qwen_blog.json").read_bytes()
    fetcher = FixtureFetcher({url: raw})

    with pytest.raises(ValueError, match="exceeds the limit"):
        await PublicJsonCollector(fetcher).collect(
            CollectContext(
                source_url="https://qwen.ai/api/v2/article/retrieval",
                config={
                    "query_params": "type=qwen_ai&language=zh-CN",
                    "max_items": 20,
                    "response_limit_bytes": 1024,
                    "items_field": "data.articles",
                    "link_template": "https://qwen.ai/blog/{path}",
                    "date_field": "date",
                    "date_nested_in": "extra",
                    "date_format": "iso",
                },
            )
        )


@pytest.mark.asyncio
async def test_infoq_collector_uses_post_api_and_filters_non_articles() -> None:
    topic_body = '{"alias": "AI&LLM"}'
    topic_key = f"https://www.infoq.cn/public/v1/topic/getInfo|{topic_body}"
    article_body = '{"id": 31, "page": 1, "size": 10}'
    article_key = f"https://www.infoq.cn/public/v1/article/getList|{article_body}"

    responses = {
        topic_key: (FIXTURES / "infoq_topic.json").read_bytes(),
        article_key: (FIXTURES / "infoq_articles.json").read_bytes(),
    }
    fetcher = FixtureFetcher(responses)

    items = await InfoQAICollector(fetcher).collect(
        CollectContext(
            source_url="https://www.infoq.cn/topic/AI%26LLM",
            config={
                "topic_alias": "AI&LLM",
                "max_pages": 1,
                "max_items": 10,
            },
        )
    )

    # 6 articles in fixture but sub_type=4 (index 3) should be filtered
    assert len(items) >= 4
    titles = [item.title for item in items]
    assert "GMI Cloud  " in titles[0] or "GMI Cloud" in titles[0]
    assert "无问芯穹" not in " ".join(titles)
    assert all(item.canonical_url.startswith("https://www.infoq.cn/article/") for item in items)
    assert all(item.published_at is not None for item in items)
    assert all(item.extra.get("collector") == "infoq_ai" for item in items)
    assert len(fetcher.post_requests) >= 2


@pytest.mark.asyncio
async def test_infoq_collector_enforces_page_limit() -> None:
    topic_body = '{"alias": "AI&LLM"}'
    topic_key = f"https://www.infoq.cn/public/v1/topic/getInfo|{topic_body}"
    article_body_p1 = '{"id": 31, "page": 1, "size": 5}'
    article_key_p1 = f"https://www.infoq.cn/public/v1/article/getList|{article_body_p1}"

    responses = {
        topic_key: (FIXTURES / "infoq_topic.json").read_bytes(),
        article_key_p1: (FIXTURES / "infoq_articles.json").read_bytes(),
    }
    fetcher = FixtureFetcher(responses)

    items = await InfoQAICollector(fetcher).collect(
        CollectContext(
            source_url="https://www.infoq.cn/topic/AI%26LLM",
            config={
                "topic_alias": "AI&LLM",
                "max_pages": 1,
                "max_items": 5,
            },
        )
    )

    assert len(items) <= 5


@pytest.mark.asyncio
async def test_rss_categories_filter_keeps_only_configured_categories() -> None:
    url = "https://www.leiphone.com/feed"
    fetcher = FixtureFetcher({url: (FIXTURES / "leiphone_feed.xml").read_bytes()})

    items = await RSSCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={"max_items": 20, "include_categories": ["人工智能", "人工智能学术"]},
        )
    )

    assert len(items) == 2
    assert items[0].title == "国产大模型正式发布新一代推理能力"
    assert items[1].title == "AI学术论文获国际顶级会议最佳论文奖"
    assert all(item.canonical_url.startswith("https://www.leiphone.com/") for item in items)
    assert all(item.published_at is not None for item in items)


@pytest.mark.asyncio
async def test_rss_categories_excludes_configured_categories() -> None:
    url = "https://www.leiphone.com/feed"
    fetcher = FixtureFetcher({url: (FIXTURES / "leiphone_feed.xml").read_bytes()})

    items = await RSSCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={"max_items": 20, "exclude_categories": ["机器人", "雷峰早报"]},
        )
    )

    assert len(items) == 3
    titles = {item.title for item in items}
    assert "机器人公司发布新一代人形机器人" not in titles


@pytest.mark.asyncio
async def test_rss_collector_no_category_filter_when_not_configured() -> None:
    url = "https://www.leiphone.com/feed"
    fetcher = FixtureFetcher({url: (FIXTURES / "leiphone_feed.xml").read_bytes()})

    items = await RSSCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert len(items) == 4


@pytest.mark.asyncio
async def test_huxiu_collector_reads_nuxt_data_with_dates_and_stable_links() -> None:
    url = "https://www.huxiu.com/article/"
    fetcher = FixtureFetcher({url: (FIXTURES / "huxiu_article.html").read_bytes()})

    items = await HuxiuCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert len(items) == 4
    assert items[0].title == "国产大模型正式发布新一代推理能力"
    assert items[0].canonical_url == "https://www.huxiu.com/article/4877051.html"
    assert items[0].published_at is not None
    assert items[0].extra["public_payload"] == "__NUXT_DATA__"
    assert items[0].extra["article_id"] == "4877051"
    assert items[1].title == "AI 智能体平台开放全新 API"
    assert items[1].canonical_url == "https://www.huxiu.com/article/4877040.html"
    assert all(item.published_at is not None for item in items)


@pytest.mark.asyncio
async def test_huxiu_collector_enforces_max_items() -> None:
    url = "https://www.huxiu.com/article/"
    fetcher = FixtureFetcher({url: (FIXTURES / "huxiu_article.html").read_bytes()})

    items = await HuxiuCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 2})
    )

    assert len(items) == 2


@pytest.mark.asyncio
async def test_huxiu_collector_handles_missing_nuxt_payload() -> None:
    url = "https://www.huxiu.com/article/"
    html = b"<!DOCTYPE html><html><head><title>Empty</title></head><body></body></html>"
    fetcher = FixtureFetcher({url: html})

    items = await HuxiuCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert items == []


@pytest.mark.asyncio
async def test_huxiu_collector_handles_invalid_json_nuxt_payload() -> None:
    url = "https://www.huxiu.com/article/"
    html = b'<script id="__NUXT_DATA__" type="application/json">not json</script>'
    fetcher = FixtureFetcher({url: html})

    items = await HuxiuCollector(fetcher).collect(
        CollectContext(source_url=url, config={"max_items": 20})
    )

    assert items == []


@pytest.mark.asyncio
async def test_ithome_selector_extracts_titles_links_and_dates_with_ai_filter() -> None:
    url = "https://www.ithome.com/list/ai.html"
    fetcher = FixtureFetcher({url: (FIXTURES / "ithome_list.html").read_bytes()})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["www.ithome.com", "ithome.com"],
                "filter_selector_items": True,
                "discovery": {
                    "mode": "selectors",
                    "include_text": ["AI", "大模型", "智能体", "模型", "开源"],
                },
                "extraction": {
                    "item_selector": "#list ul.datel li",
                    "title_selector": "a.t",
                    "link_selector": "a.t",
                    "date_selector": "i",
                },
            },
        )
    )

    assert len(items) >= 2
    titles = [item.title for item in items]
    assert "百时美施贵宝采购英伟达最新 AI 计算系统, 加速药物研发" in titles
    assert "国产大模型平台新增 Agent 编排能力" in titles
    assert "开源大模型社区发布多项能力基准评测结果" in titles
    assert "漫威预告复仇者联盟 5 将于年底上映" not in titles
    assert all(item.published_at is not None for item in items)
    assert all(
        item.canonical_url.startswith("https://www.ithome.com/") for item in items
    )
    assert items[0].extra["original_time_text"] == "2026-07-21 15:50:05"


@pytest.mark.asyncio
async def test_ithome_selector_respects_max_items() -> None:
    url = "https://www.ithome.com/list/ai.html"
    fetcher = FixtureFetcher({url: (FIXTURES / "ithome_list.html").read_bytes()})

    items = await HTMLListCollector(fetcher).collect(
        CollectContext(
            source_url=url,
            config={
                "allowed_domains": ["www.ithome.com"],
                "discovery": {"mode": "selectors", "max_items": 2},
                "extraction": {
                    "item_selector": "#list ul.datel li",
                    "title_selector": "a.t",
                    "link_selector": "a.t",
                    "date_selector": "i",
                },
            },
        )
    )

    assert len(items) <= 2
