# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Stage 12 production UI regression and completion coverage."""

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.classifiers.providers import LLMResponse, OpenAICompatibleProvider
from app.domain.enums import (
    Category,
    CrawlStatus,
    LifecycleState,
    RunTrigger,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceTier,
    SourceType,
)
from app.domain.models import CrawlRun, CrawlSourceExecution, IntelligenceItem, Source
from app.domain.update import (
    SourceUpdateResult,
    SourceUpdateStatus,
    UpdateResult,
)
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app
from app.web.schemas import ItemQueryParams


@pytest.fixture
def stage12_client(database: Database) -> Iterator[TestClient]:
    with TestClient(
        create_app(database=database, enforce_migrations=False),
        raise_server_exceptions=False,
    ) as client:
        yield client


def _source(
    name: str = "正式来源", *, lifecycle_state: LifecycleState = LifecycleState.ACTIVE
) -> Source:
    host_token = name.encode("utf-8").hex()[:32]
    return Source(
        name=name,
        source_type=SourceType.RSS,
        start_url=f"https://source-{host_token}.example/feed",
        enabled=lifecycle_state is LifecycleState.ACTIVE,
        lifecycle_state=lifecycle_state,
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
        source_kind=SourceKind.FORMAL,
        source_tier=SourceTier.OFFICIAL_COMPANY,
        audience=SourceAudience.LEADERSHIP,
        homepage_visible=True,
        export_visible=True,
    )


@pytest.mark.parametrize("category", tuple(Category), ids=lambda value: value.value)
def test_every_rendered_category_value_is_accepted(
    stage12_client: TestClient, category: Category
) -> None:
    page = stage12_client.get("/")
    assert f'value="{category.value}"' in page.text

    response = stage12_client.get(
        "/",
        params={"category": category.value, "time_range": "all"},
    )

    assert response.status_code == 200
    assert "筛选参数无效" not in response.text


@pytest.mark.parametrize(
    "extra",
    (
        {"keyword": "模型"},
        {"source_id": "1"},
        {"is_read": "no"},
        {"discovered_from": "2026-07-01", "discovered_to": "2026-07-22"},
    ),
)
def test_category_combines_with_other_filters_without_400(
    stage12_client: TestClient, extra: dict[str, str]
) -> None:
    query = {"category": Category.MODEL_TECHNOLOGY.value, "time_range": "all", **extra}
    assert stage12_client.get("/", params=query).status_code == 200


@pytest.mark.parametrize("time_range", ("all", "today", "3d", "7d", "30d"))
def test_quick_time_ranges_have_valid_server_bounds(time_range: str) -> None:
    now = datetime(2026, 7, 22, 15, 30, tzinfo=UTC)
    query = ItemQueryParams.parse({"time_range": time_range}).to_filter(now=now)
    if time_range == "all":
        assert query.published_from is None and query.published_to is None
    else:
        assert query.published_to == datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
        assert query.published_from is not None
        assert query.published_to is not None
        assert query.published_from < query.published_to


def test_custom_dates_deterministically_override_quick_range() -> None:
    params = ItemQueryParams.parse(
        {"time_range": "7d", "published_from": "2026-07-01", "published_to": "2026-07-02"}
    )
    query = params.to_filter(now=datetime(2026, 7, 22, tzinfo=UTC))

    assert params.time_range == "all"
    assert query.published_from == datetime(2026, 6, 30, 16, 0, tzinfo=UTC)
    assert query.published_to == datetime(2026, 7, 2, 16, 0, tzinfo=UTC)


def test_real_original_links_render_in_both_views_and_missing_url_is_not_linked(
    database: Database, stage12_client: TestClient
) -> None:
    source = _source()
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="带查询参数的原文",
                original_url="https://news.example/story?id=7&from=feed",
                canonical_url="https://news.example/story?id=7&from=feed",
                fingerprint="1" * 64,
            )
        )
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="没有可用网址",
                original_url="",
                canonical_url="https://news.example/missing",
                fingerprint="2" * 64,
            )
        )

    response = stage12_client.get("/")
    href = 'href="https://news.example/story?id=7&amp;from=feed"'
    cards = response.text.split('id="list-a"', 1)[1].split('id="list-b"', 1)[0]
    compact = response.text.split('id="list-b"', 1)[1]

    assert href in cards and href in compact
    assert 'target="_blank"' in cards and 'target="_blank"' in compact
    assert 'rel="noopener noreferrer"' in cards and 'rel="noopener noreferrer"' in compact
    assert 'href="#"' not in response.text
    assert '<a href="">没有可用网址</a>' not in response.text


def test_home_overview_uses_real_counts(database: Database, stage12_client: TestClient) -> None:
    source = _source()
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="今日待分类",
                original_url="https://stats.example/today",
                canonical_url="https://stats.example/today",
                category=Category.UNCLASSIFIED,
                discovered_at=datetime.now(UTC),
                fingerprint="3" * 64,
                is_read=False,
            )
        )

    response = stage12_client.get("/")
    assert response.status_code == 200
    for label in ("全部资讯", "未读", "今日新增", "待分类"):
        assert f'<span class="stat-label">{label}</span>' in response.text
    assert "—</span><span class=\"stat-label\">未读" not in response.text


def test_ai_connection_uses_mock_provider_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch, stage12_client: TestClient
) -> None:
    async def fake_classify(
        _provider: OpenAICompatibleProvider,
        _title: str,
        _summary: str | None,
        _source_name: str,
        _source_role: str | None,
    ) -> LLMResponse:
        return LLMResponse(
            category=Category.MODEL_TECHNOLOGY,
            confidence=0.91,
            reason="测试连接",
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "classify", fake_classify)
    secret = "test-secret-must-not-render"
    response = stage12_client.post(
        "/ai/test-connection",
        data={
            "base_url": "https://mock.example",
            "model": "mock-model",
            "api_key": secret,
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 200
    assert "连接成功" in response.text
    assert secret not in response.text


class _RecordingPipeline(UpdatePipeline):
    def __init__(self, result: UpdateResult) -> None:
        self.result = result

    async def update(self, **_kwargs: object) -> UpdateResult:
        return self.result


def test_update_result_uses_chinese_summary_and_detailed_source_table(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    result = UpdateResult(
        crawl_run_id=12,
        status=CrawlStatus.PARTIAL_SUCCESS,
        started_at=now,
        finished_at=now,
        source_total=2,
        source_success=1,
        source_failed=1,
        discovered_count=4,
        new_count=1,
        updated_count=1,
        skipped_count=1,
        unclassified_count=1,
        error_summary="一个来源失败",
        source_results=(
            SourceUpdateResult(
                source_id=1,
                source_name="真实来源",
                status=SourceUpdateStatus.SUCCESS,
                discovered=4,
                normalized=4,
                accepted=3,
                rejected=1,
                classified=3,
                new=1,
                updated=1,
                duplicate=1,
                rejection_reason_counts={"quality.below_minimum": 1},
            ),
        ),
        normalized_count=4,
        accepted_count=3,
        rejected_count=1,
        classified_count=3,
        duplicate_count=1,
        rejection_reason_counts={"quality.below_minimum": 1},
    )
    pipeline = _RecordingPipeline(result)

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    app = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(app) as client:
        response = client.post("/updates")

    assert response.status_code == 200
    for label in (
        "更新结果摘要",
        "成功来源",
        "失败来源",
        "通过准入",
        "未通过准入",
        "来源明细",
        "主要原因与错误",
        "返回资讯",
        "查看更新记录",
    ):
        assert label in response.text
    for internal in (
        "fetched / normalized",
        "accepted / rejected",
        "inserted / updated / duplicate",
    ):
        assert internal not in response.text


def test_candidate_tab_loads_real_candidate_data(
    database: Database, stage12_client: TestClient
) -> None:
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(_source("真实候选", lifecycle_state=LifecycleState.CANDIDATE))

    active = stage12_client.get("/sources")
    candidate = stage12_client.get("/sources", params={"tab": "candidate"})

    assert "真实候选" not in active.text
    assert candidate.status_code == 200
    assert "真实候选" in candidate.text
    assert "预览" in candidate.text and "启用并加入监控" in candidate.text


def test_source_detail_has_real_stats_runs_items_and_errors(
    database: Database, stage12_client: TestClient
) -> None:
    source = _source("详情来源")
    source.last_error = "最近网络失败"
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        run = uow.crawl_runs.add(
            CrawlRun(
                status=CrawlStatus.PARTIAL_SUCCESS,
                trigger=RunTrigger.MANUAL_WEB,
                source_total=1,
                source_failed=1,
            )
        )
        uow.crawl_source_executions.add(
            CrawlSourceExecution(
                crawl_run_id=run.id,
                source_id=source.id,
                status="failed",
                discovered_count=2,
                accepted_count=1,
                rejected_count=1,
                new_count=1,
                failed_count=1,
                error="来源执行失败",
            )
        )
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="最近资讯",
                original_url="https://detail.example/item?x=1&y=2",
                canonical_url="https://detail.example/item?x=1&y=2",
                fingerprint="4" * 64,
            )
        )

    response = stage12_client.get(f"/sources/{source.id}")
    assert response.status_code == 200
    for label in (
        "近 30 天统计",
        "通过准入",
        "未通过准入",
        "最近运行",
        "最近抓取的资讯",
        "最近网络失败",
        "来源执行失败",
        "最近资讯",
    ):
        assert label in response.text


def test_runs_filters_and_source_detail_are_real(
    database: Database, stage12_client: TestClient
) -> None:
    source = _source("运行来源")
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        success = uow.crawl_runs.add(
            CrawlRun(
                status=CrawlStatus.SUCCESS,
                trigger=RunTrigger.MANUAL_WEB,
                source_total=1,
                source_success=1,
                discovered_count=3,
            )
        )
        uow.crawl_source_executions.add(
            CrawlSourceExecution(
                crawl_run_id=success.id,
                source_id=source.id,
                status="success",
                discovered_count=3,
                accepted_count=2,
                rejected_count=1,
                new_count=2,
            )
        )
        failed = uow.crawl_runs.add(
            CrawlRun(status=CrawlStatus.FAILED, trigger=RunTrigger.SCHEDULED)
        )

    response = stage12_client.get(
        "/runs",
        params={"status": "success", "trigger": "manual_web", "per_page": "20"},
    )
    assert response.status_code == 200
    assert "运行来源" in response.text
    assert "来源明细" in response.text
    assert f"运行 #{failed.id}" not in response.text
    assert "status=success" in response.text or 'value="success" selected' in response.text
