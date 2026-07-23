# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Stage-five local Web UI behavior using only temporary databases."""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import cli
from app.domain.enums import (
    Category,
    CrawlStatus,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceTier,
    SourceType,
)
from app.domain.models import CrawlRun, IntelligenceItem, Source
from app.domain.update import (
    SourcePreviewItem,
    SourcePreviewResult,
    SourceUpdateResult,
    SourceUpdateStatus,
    UpdateResult,
)
from app.services.source_seed_service import SourceSeedService
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app
from app.web.dependencies import UpdateInProgressError, WebUpdateService


@pytest.fixture
def web_app(database: Database) -> FastAPI:
    return create_app(database=database, enforce_migrations=False)


@pytest.fixture
def client(web_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(web_app, raise_server_exceptions=False) as test_client:
        yield test_client


def _source(name: str, url: str, *, enabled: bool = True) -> Source:
    return Source(
        name=name,
        source_type=SourceType.RSS,
        start_url=url,
        enabled=enabled,
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
        source_kind=SourceKind.FORMAL,
        source_tier=SourceTier.OFFICIAL_COMPANY,
        audience=SourceAudience.LEADERSHIP,
        homepage_visible=True,
        export_visible=True,
    )


def _seed_content(database: Database, *, count: int = 3) -> tuple[Source, Source, list[int]]:
    first = _source("第一来源", "https://one.example/feed")
    second = _source("第二来源", "https://two.example/feed")
    now = datetime(2026, 7, 18, 8, 0, tzinfo=UTC)
    ids: list[int] = []
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(first)
        uow.sources.add(second)
        for index in range(count):
            source = first if index % 2 == 0 else second
            item = uow.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    title=f"行业标题 {index}",
                    original_url=f"https://articles.example/{index}",
                    canonical_url=f"https://articles.example/{index}",
                    summary="中文简介与 Agent 进展" if index != 1 else None,
                    published_at=now - timedelta(days=index) if index != 2 else None,
                    discovered_at=now - timedelta(hours=index),
                    last_seen_at=now - timedelta(minutes=index),
                    category=(
                        Category.MODEL_TECHNOLOGY if index % 2 == 0 else Category.UNCLASSIFIED
                    ),
                    manual_category=(Category.AWARD_CASE if index == 0 else None),
                    fingerprint=f"{index:064x}",
                    is_favorite=index == 0,
                )
            )
            ids.append(item.id)
    return first, second, ids


def _result(*, run_id: int = 1) -> UpdateResult:
    now = datetime.now(UTC)
    return UpdateResult(
        crawl_run_id=run_id,
        status=CrawlStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        source_total=2,
        source_success=2,
        source_failed=0,
        discovered_count=8,
        new_count=3,
        updated_count=1,
        skipped_count=4,
        unclassified_count=2,
        error_summary=None,
        source_results=(),
    )


def test_home_empty_state_and_navigation(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert '<html lang="zh-CN">' in response.text
    assert "暂时没有符合条件的资讯" in response.text
    assert "立即更新" in response.text
    assert all(label in response.text for label in ("资讯", "来源", "更新记录"))


def test_home_lists_fields_manual_priority_and_safe_links(
    database: Database, client: TestClient
) -> None:
    _seed_content(database)

    response = client.get("/")

    assert response.status_code == 200
    assert "行业标题 0" in response.text
    assert "第一来源" in response.text
    assert "获奖与优秀案例" in response.text
    assert "人工" in response.text
    assert "发布时间未知" in response.text
    assert "中文简介与 Agent 进展" in response.text
    assert 'target="_blank"' in response.text
    assert 'rel="noopener noreferrer"' in response.text
    assert "<img" not in response.text


def test_jinja_escapes_source_title_summary_and_error(
    database: Database, client: TestClient
) -> None:
    source = _source("<b>来源</b>", "https://escape.example/feed")
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="<script>alert(1)</script>",
                summary="<img src=x onerror=alert(2)>",
                original_url="https://escape.example/item",
                canonical_url="https://escape.example/item",
                fingerprint="e" * 64,
            )
        )
        uow.crawl_runs.add(
            CrawlRun(
                status=CrawlStatus.FAILED,
                source_total=1,
                source_failed=1,
                error_summary="<script>Traceback token=top-secret</script>",
            )
        )

    home = client.get("/")
    runs = client.get("/runs")

    assert "<script>" not in home.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in home.text
    assert "&lt;img src=x onerror=alert(2)&gt;" in home.text
    assert "Traceback" not in runs.text
    assert "top-secret" not in runs.text


def test_database_pagination_stable_and_capped(database: Database, client: TestClient) -> None:
    _seed_content(database, count=23)

    first_page = client.get("/?per_page=20")
    second_page = client.get("/?per_page=20&page=2")
    excessive = client.get("/?per_page=101")

    assert "第 1 / 2 页 · 共 23 条" in first_page.text
    assert "第 2 / 2 页 · 共 23 条" in second_page.text
    assert "行业标题 22" in second_page.text
    assert excessive.status_code == 400


def test_page_beyond_available_results_returns_clear_400(
    database: Database, client: TestClient
) -> None:
    _seed_content(database, count=23)

    response = client.get("/?per_page=20&page=3")

    assert response.status_code == 400
    assert "页码超出范围" in response.text


@pytest.mark.parametrize(
    ("query", "included", "excluded"),
    [
        ("category=award_case", "行业标题 0", "行业标题 1"),
        ("source_id=1", "行业标题 0", "行业标题 1"),
        ("favorite=yes", "行业标题 0", "行业标题 2"),
        ("unclassified=yes", "行业标题 1", "行业标题 0"),
        ("keyword=Agent", "行业标题 0", "行业标题 1"),
        ("published_from=2026-07-18", "行业标题 0", "行业标题 1"),
        ("discovered_to=2026-07-17", "暂时没有符合条件的资讯", "行业标题 0"),
    ],
)
def test_item_filters(
    database: Database,
    client: TestClient,
    query: str,
    included: str,
    excluded: str,
) -> None:
    _seed_content(database)

    response = client.get(f"/?{query}")

    assert response.status_code == 200
    assert included in response.text
    assert excluded not in response.text


def test_filters_combine_with_and_and_pagination_preserves_values(
    database: Database, client: TestClient
) -> None:
    _seed_content(database, count=45)

    response = client.get("/?source_id=1&favorite=no&keyword=%E4%B8%AD%E6%96%87&per_page=20")

    assert response.status_code == 200
    assert "source_id=1" in response.text
    assert "favorite=no" in response.text
    assert "keyword=%E4%B8%AD%E6%96%87" in response.text
    assert "行业标题 2" in response.text
    assert ">行业标题 1</a>" not in response.text


@pytest.mark.parametrize(
    "query",
    [
        "category=not-real",
        "source_id=-1",
        "source_id=9223372036854775808",
        "favorite=maybe",
        "published_from=2026-99-99",
        "published_from=2026-07-19&published_to=2026-07-18",
        "published_to=9999-12-31",
        "page=0",
        f"keyword={'x' * 201}",
        "unexpected=value",
    ],
)
def test_invalid_filters_return_clear_400(client: TestClient, query: str) -> None:
    response = client.get(f"/?{query}")

    assert response.status_code == 400
    assert "操作未完成" in response.text
    assert "Traceback" not in response.text


def test_blank_filter_form_values_are_ignored(client: TestClient) -> None:
    response = client.get(
        "/?keyword=+&category=&source_id=&published_from=&published_to="
        "&discovered_from=&discovered_to=&favorite=all&unclassified=all&per_page=20"
    )

    assert response.status_code == 200
    assert "暂时没有符合条件的资讯" in response.text


@pytest.mark.parametrize("keyword", ["%", "_", "\\", "'", '"', "中文"])
def test_special_search_characters_are_safe(
    database: Database, client: TestClient, keyword: str
) -> None:
    _seed_content(database)

    response = client.get("/", params={"keyword": keyword})

    assert response.status_code == 200
    assert "SQLAlchemy" not in response.text


@pytest.mark.parametrize("keyword", ["%", "_", "\\"])
def test_like_wildcards_and_escape_character_are_matched_literally(
    database: Database, client: TestClient, keyword: str
) -> None:
    source = _source("符号来源", "https://literal.example/feed")
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="包含字面符号 100%_路径\\文件",
                original_url="https://literal.example/special",
                canonical_url="https://literal.example/special",
                fingerprint="c" * 64,
            )
        )
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="不含特殊符号的普通标题",
                original_url="https://literal.example/plain",
                canonical_url="https://literal.example/plain",
                fingerprint="d" * 64,
            )
        )

    response = client.get("/", params={"keyword": keyword})

    assert response.status_code == 200
    assert "包含字面符号" in response.text
    assert "不含特殊符号" not in response.text


def test_favorite_and_unfavorite_are_idempotent_and_persisted(
    database: Database, client: TestClient
) -> None:
    _, _, item_ids = _seed_content(database)
    item_id = item_ids[1]

    assert (
        client.post(
            f"/items/{item_id}/favorite",
            data={"favorite": "true", "return_to": "/?favorite=yes"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    client.post(f"/items/{item_id}/favorite", data={"favorite": "true"})
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.get(item_id).is_favorite is True  # type: ignore[union-attr]
    client.post(f"/items/{item_id}/favorite", data={"favorite": "false"})
    client.post(f"/items/{item_id}/favorite", data={"favorite": "false"})
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.get(item_id).is_favorite is False  # type: ignore[union-attr]


def test_manual_category_and_clear_only_change_manual_fields(
    database: Database, client: TestClient
) -> None:
    _, _, item_ids = _seed_content(database)
    item_id = item_ids[1]
    with RepositoryUnitOfWork(database) as uow:
        before = uow.items.get(item_id)
        assert before is not None
        last_seen = before.last_seen_at
        automatic = before.category
        score = before.classification_score
        reason = before.classification_reason

    response = client.post(f"/items/{item_id}/category", data={"category": "enterprise_case"})
    assert response.status_code == 200
    assert "企业成果与应用案例" in response.text
    assert "人工" in response.text
    with RepositoryUnitOfWork(database) as uow:
        changed = uow.items.get(item_id)
        assert changed is not None
        assert changed.manual_category is Category.ENTERPRISE_CASE
        assert changed.category is automatic
        assert changed.classification_score == score
        assert changed.classification_reason == reason
        assert changed.last_seen_at == last_seen
        assert uow.revisions.list() == []

    cleared = client.post(f"/items/{item_id}/category", data={"category": ""})
    assert "待分类" in cleared.text
    assert "自动" in cleared.text
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.get(item_id).manual_category is None  # type: ignore[union-attr]


def test_invalid_manual_category_returns_400_without_mutation(
    database: Database, client: TestClient
) -> None:
    _, _, item_ids = _seed_content(database)

    response = client.post(f"/items/{item_ids[1]}/category", data={"category": "made_up"})

    assert response.status_code == 400
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.get(item_ids[1]).manual_category is None  # type: ignore[union-attr]


def test_source_enable_disable_and_no_delete_or_config_edit(
    database: Database, client: TestClient
) -> None:
    first, _, _ = _seed_content(database)

    disabled = client.post(
        f"/sources/{first.id}/enabled", data={"enabled": "false"}, follow_redirects=False
    )
    assert disabled.status_code == 303
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.sources.get(first.id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.collector_config == {}
    assert client.delete(f"/sources/{first.id}").status_code in {404, 405}
    assert client.patch(f"/sources/{first.id}", json={"collector_config": {}}).status_code in {
        404,
        405,
    }
    client.post(f"/sources/{first.id}/enabled", data={"enabled": "true"})
    with RepositoryUnitOfWork(database) as uow:
        assert uow.sources.get(first.id).enabled is True  # type: ignore[union-attr]


def test_missing_mutation_targets_return_404(client: TestClient) -> None:
    assert client.post("/items/999/favorite", data={"favorite": "true"}).status_code == 404
    assert client.post("/items/999/category", data={"category": "award_case"}).status_code == 404
    assert client.post("/sources/999/enabled", data={"enabled": "true"}).status_code == 404
    assert client.post("/sources/999/updates").status_code == 404


def test_out_of_range_path_ids_return_400(client: TestClient) -> None:
    huge = 9_223_372_036_854_775_808
    assert client.post(f"/items/{huge}/favorite", data={"favorite": "true"}).status_code == 400
    assert client.post(f"/sources/{huge}/updates").status_code == 400


def test_unsafe_return_path_cannot_redirect_off_origin(
    database: Database, client: TestClient
) -> None:
    _, _, item_ids = _seed_content(database)

    response = client.post(
        f"/items/{item_ids[0]}/favorite",
        data={"favorite": "true", "return_to": "/\\evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_disabled_source_update_returns_400_without_run(
    database: Database, client: TestClient
) -> None:
    source = _source("停用来源", "https://disabled.example/feed", enabled=False)
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)

    response = client.post(f"/sources/{source.id}/updates")

    assert response.status_code == 400
    with RepositoryUnitOfWork(database) as uow:
        assert uow.crawl_runs.list() == []


def test_write_failure_rolls_back_and_hides_database_details(
    database: Database, client: TestClient
) -> None:
    _, _, item_ids = _seed_content(database)
    item_id = item_ids[1]

    def fail_item_update(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if statement.lstrip().upper().startswith("UPDATE INTELLIGENCE_ITEMS"):
            raise OperationalError(
                "UPDATE intelligence_items SET secret='database-path'",
                {},
                RuntimeError("/private/formal/intelligence.db token=top-secret"),
            )

    event.listen(database.engine, "before_cursor_execute", fail_item_update)
    try:
        response = client.post(
            f"/items/{item_id}/favorite",
            data={"favorite": "true"},
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", fail_item_update)

    assert response.status_code == 500
    assert "UPDATE intelligence_items" not in response.text
    assert "intelligence.db" not in response.text
    assert "top-secret" not in response.text
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.get(item_id).is_favorite is False  # type: ignore[union-attr]


def test_all_write_routes_reject_get(database: Database, client: TestClient) -> None:
    first, _, item_ids = _seed_content(database)

    paths = (
        f"/items/{item_ids[0]}/favorite",
        f"/items/{item_ids[0]}/category",
        f"/sources/{first.id}/enabled",
        "/updates",
        f"/sources/{first.id}/updates",
        f"/sources/{first.id}/preview",
    )
    assert all(client.get(path).status_code == 405 for path in paths)


class RecordingPipeline(UpdatePipeline):
    def __init__(self, result: UpdateResult) -> None:
        self.result = result
        self.calls: list[int | None] = []

    async def update(self, *, source_id: int | None = None, **_kwargs: object) -> UpdateResult:
        self.calls.append(source_id)
        return self.result


def test_web_updates_call_shared_pipeline_and_render_result(database: Database) -> None:
    source = _source("来源", "https://update.example/feed")
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
    pipeline = RecordingPipeline(_result())

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(application) as update_client:
        all_response = update_client.post("/updates")
        one_response = update_client.post(f"/sources/{source.id}/updates")

    assert pipeline.calls == [None, source.id]
    for response in (all_response, one_response):
        assert response.status_code == 200
        assert "更新结果" in response.text
        assert "3" in response.text
        assert "/runs#run-1" in response.text


def test_web_update_result_does_not_label_fetch_failure_as_rejection(
    database: Database,
) -> None:
    now = datetime.now(UTC)
    result = UpdateResult(
        crawl_run_id=7,
        status=CrawlStatus.FAILED,
        started_at=now,
        finished_at=now,
        source_total=1,
        source_success=0,
        source_failed=1,
        discovered_count=0,
        new_count=0,
        updated_count=0,
        skipped_count=0,
        unclassified_count=0,
        error_summary="network unavailable",
        source_results=(
            SourceUpdateResult(
                source_id=1,
                source_name="网络来源",
                status=SourceUpdateStatus.FAILED,
                failed=1,
                failure_reason_counts={"fetch.failed": 1},
                error="network unavailable",
            ),
        ),
        failed_count=1,
        failure_reason_counts={"fetch.failed": 1},
    )
    pipeline = RecordingPipeline(result)

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(application) as update_client:
        response = update_client.post("/updates")

    assert response.status_code == 200
    assert "失败" in response.text
    assert "网络抓取失败" in response.text
    assert "准入拒绝" not in response.text


def test_saved_source_preview_page_shows_primary_rejection_reason(database: Database) -> None:
    source = _source("预览来源", "https://preview.example/feed")
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)

    class PreviewPipeline(RecordingPipeline):
        async def preview(self, source_id: int, *, max_items: int = 10) -> SourcePreviewResult:
            del max_items
            assert source_id == source.id
            return SourcePreviewResult(
                source_id=source.id,
                source_name=source.name,
                status=SourceUpdateStatus.SUCCESS,
                fetched=1,
                normalized=1,
                rejected=1,
                rejection_reason_counts={"quality.below_minimum": 1},
                items=(
                    SourcePreviewItem(
                        title="普通动态",
                        original_url="https://preview.example/item",
                        accepted=False,
                        reason="quality.below_minimum",
                        quality_score=30,
                    ),
                ),
            )

    pipeline = PreviewPipeline(_result())

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(application) as preview_client:
        response = preview_client.post(f"/sources/{source.id}/preview")

    assert response.status_code == 200
    assert "拒绝主因" in response.text
    assert "质量分低于来源门槛" in response.text
    assert "不落库预览" in response.text


def test_update_result_error_is_sanitized_escaped_and_truncated(database: Database) -> None:
    pipeline = RecordingPipeline(
        replace(
            _result(),
            error_summary="<script>Traceback token=top-secret</script> " + "x" * 500,
        )
    )

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(application) as update_client:
        response = update_client.post("/updates")

    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "top-secret" not in response.text
    assert "Traceback" not in response.text
    assert "x" * 301 not in response.text


class BlockingPipeline(UpdatePipeline):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release

    async def update(self, **_kwargs: object) -> UpdateResult:
        self.started.set()
        await self.release.wait()
        return _result()


@pytest.mark.asyncio
async def test_update_lock_rejects_concurrency(database: Database) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    pipeline = BlockingPipeline(started, release)

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    service = WebUpdateService(database, context)
    first = asyncio.create_task(service.update())
    await started.wait()
    with pytest.raises(UpdateInProgressError):
        await service.update()
    release.set()
    await first


@pytest.mark.asyncio
async def test_two_simultaneous_http_updates_return_success_and_409(database: Database) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    pipeline = BlockingPipeline(started, release)

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    transport = ASGITransport(app=application, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as update_client:
        first = asyncio.create_task(update_client.post("/updates"))
        await started.wait()
        second = await update_client.post("/updates")
        release.set()
        first_response = await first

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert "已有更新正在运行" in second.text


@pytest.mark.asyncio
async def test_update_lock_releases_after_exception(database: Database) -> None:
    calls = 0

    class SometimesFailingPipeline(UpdatePipeline):
        async def update(self, **_kwargs: object) -> UpdateResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("token=secret Traceback <html>failure</html>")
            return _result()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield SometimesFailingPipeline.__new__(SometimesFailingPipeline)

    service = WebUpdateService(database, context)
    with pytest.raises(RuntimeError):
        await service.update()
    assert (await service.update()).status is CrawlStatus.SUCCESS


def test_pipeline_exception_does_not_break_web_and_lock_is_released(database: Database) -> None:
    calls = 0

    class SometimesFailingPipeline(UpdatePipeline):
        async def update(self, **_kwargs: object) -> UpdateResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("Traceback token=top-secret /private/formal/intelligence.db")
            return _result()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield SometimesFailingPipeline.__new__(SometimesFailingPipeline)

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
    )
    with TestClient(application, raise_server_exceptions=False) as update_client:
        failed = update_client.post("/updates")
        home = update_client.get("/")
        succeeded = update_client.post("/updates")

    assert failed.status_code == 500
    assert "top-secret" not in failed.text
    assert "intelligence.db" not in failed.text
    assert home.status_code == 200
    assert succeeded.status_code == 200


def test_runs_page_is_latest_first_paginated_and_sanitized(
    database: Database, client: TestClient
) -> None:
    now = datetime.now(UTC)
    with RepositoryUnitOfWork(database) as uow:
        for index in range(22):
            uow.crawl_runs.add(
                CrawlRun(
                    started_at=now + timedelta(minutes=index),
                    status=CrawlStatus.RUNNING if index == 21 else CrawlStatus.SUCCESS,
                    source_total=1,
                    error_summary="password=secret <b>bad</b>" if index == 21 else None,
                )
            )

    response = client.get("/runs?per_page=20")

    assert response.status_code == 200
    assert "第 1 / 2 页 · 共 22 条" in response.text
    assert "运行中" in response.text
    assert "secret" not in response.text
    assert response.text.index("#22") < response.text.index("#21")


def test_item_query_uses_bounded_selects_without_n_plus_one(
    database: Database, client: TestClient
) -> None:
    _seed_content(database, count=20)
    select_count = 0

    def count_selects(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(database.engine, "before_cursor_execute", count_selects)
    try:
        response = client.get("/")
    finally:
        event.remove(database.engine, "before_cursor_execute", count_selects)

    assert response.status_code == 200
    # count, joined page data, source options, and one bounded overview aggregation
    assert select_count == 4


def test_seed_sources_is_idempotent_and_does_not_overwrite_existing(database: Database) -> None:
    service = SourceSeedService(lambda: RepositoryUnitOfWork(database))

    first_result = service.seed()
    assert (first_result.created, first_result.promoted, first_result.existing) == (26, 0, 0)
    with RepositoryUnitOfWork(database) as uow:
        nda = uow.sources.get_by_slug("nda-news")
        assert nda is not None
        nda.name = "用户保留名称"
        nda.enabled = False
    second_result = service.seed()
    assert (second_result.created, second_result.existing, second_result.conflicts) == (0, 25, 1)
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()
        nda = uow.sources.get_by_slug("nda-news")
    assert len(sources) == 26
    assert nda is not None
    assert nda.name == "用户保留名称"
    assert nda.enabled is False
    assert all("Qwen-Agent" not in source.name for source in sources)


def test_seed_does_not_duplicate_equivalent_user_url(database: Database) -> None:
    existing = _source("用户来源", "https://openai.com/news/rss.xml/", enabled=False)
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(existing)

    service = SourceSeedService(lambda: RepositoryUnitOfWork(database))

    result = service.seed()
    assert (result.created, result.existing, result.conflicts) == (26, 0, 0)
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()
    assert len(sources) == 27
    assert sum(source.name == "用户来源" for source in sources) == 1
    assert next(source for source in sources if source.name == "用户来源").enabled is False


def test_seed_sources_cli_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'seed-cli.db').as_posix()}"
    initial = Database(database_url)
    initial.create_schema()
    initial.dispose()

    def database_from_settings(_cls: type[Database]) -> Database:
        return Database(database_url)

    monkeypatch.setattr(cli.Database, "from_settings", classmethod(database_from_settings))
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    assert cli.main(["sources", "seed-formal"]) == 0
    assert cli.main(["sources", "seed-formal"]) == 0
    verification = Database(database_url)
    try:
        with RepositoryUnitOfWork(verification) as uow:
            assert len(uow.sources.list()) == 26
    finally:
        verification.dispose()
    output = capsys.readouterr().out
    assert "created=26 updated=0 existing=0" in output
    assert "created=0 updated=0 existing=26" in output


def test_sources_page_can_idempotently_initialize_formal_sources(
    database: Database, client: TestClient
) -> None:
    before = client.get("/sources")
    first = client.post("/sources/seed-formal")
    second = client.post("/sources/seed-formal")

    assert "当前数据库只有 0 / 26 个目录来源" in before.text
    assert first.status_code == 200
    assert "新建 26" in first.text
    assert second.status_code == 200
    assert "新建 0" in second.text
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()
    assert len(sources) == 26
    assert all(source.source_kind is SourceKind.FORMAL for source in sources)


def test_seed_does_not_promote_modified_legacy_aiia(database: Database) -> None:
    legacy = Source(
        name="用户改名 AIIA",
        source_type=SourceType.HTML_LIST,
        start_url="https://www.aiiaorg.cn/",
        enabled=False,
        default_category=Category.POLICY_INDUSTRY,
        collector_name="html_list",
        collector_config={},
        origin=SourceOrigin.PRESET,
    )
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(legacy)

    result = SourceSeedService(lambda: RepositoryUnitOfWork(database)).seed()

    assert (result.created, result.promoted, result.conflicts) == (25, 0, 1)
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.sources.get(legacy.id)
    assert stored is not None
    assert stored.name == "用户改名 AIIA"
    assert stored.enabled is False
    assert stored.source_kind is SourceKind.TEST


def test_web_test_database_is_not_formal_database(database: Database) -> None:
    database_path = cast(str, database.engine.url.database)
    assert Path(database_path).name == "test.db"
    assert Path(database_path).resolve() != Path("data/intelligence.db").resolve()
