# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Read-state filtering and update-run filter layout regressions."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.config.settings import PROJECT_ROOT
from app.domain.enums import (
    Category,
    CrawlStatus,
    RunTrigger,
    SourceOrigin,
    SourceScope,
    SourceType,
)
from app.domain.models import CrawlRun, CrawlSourceExecution, IntelligenceItem, Source
from app.domain.queries import ItemQuery
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app


@pytest.fixture
def read_client(database: Database) -> Iterator[TestClient]:
    with TestClient(
        create_app(database=database, enforce_migrations=False),
        raise_server_exceptions=False,
    ) as client:
        yield client


@pytest.fixture
def read_data(database: Database) -> tuple[Source, Source, list[int]]:
    first = Source(
        name="阅读来源甲",
        source_type=SourceType.RSS,
        start_url="https://read-a.example/feed",
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
    )
    second = Source(
        name="阅读来源乙",
        source_type=SourceType.RSS,
        start_url="https://read-b.example/feed",
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
    )
    ids: list[int] = []
    now = datetime(2026, 7, 22, 4, 0, tzinfo=UTC)
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(first)
        uow.sources.add(second)
        for index in range(25):
            source = first if index % 2 == 0 else second
            item = uow.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    title=f"阅读筛选记录 {index:02d}",
                    original_url=f"https://articles.example/read-{index}",
                    canonical_url=f"https://articles.example/read-{index}",
                    published_at=now - timedelta(minutes=index),
                    discovered_at=now - timedelta(minutes=index),
                    last_seen_at=now,
                    category=(
                        Category.MODEL_TECHNOLOGY
                        if index % 2 == 0
                        else Category.POLICY_INDUSTRY
                    ),
                    fingerprint=f"{index + 1000:064x}",
                    is_read=index < 3,
                )
            )
            ids.append(item.id)
    return first, second, ids


def _total(html: str) -> int:
    match = re.search(r"共 <strong>(\d+)</strong> 条资讯", html)
    assert match is not None
    return int(match.group(1))


def test_read_and_unread_routes_return_disjoint_real_rows(
    read_client: TestClient,
    read_data: tuple[Source, Source, list[int]],
) -> None:
    all_page = read_client.get("/", params={"is_read": "all", "source_scope": "all"})
    read_page = read_client.get("/", params={"is_read": "yes", "source_scope": "all"})
    unread_page = read_client.get(
        "/", params={"is_read": "no", "source_scope": "all", "per_page": 100}
    )

    assert (_total(all_page.text), _total(read_page.text), _total(unread_page.text)) == (
        25,
        3,
        22,
    )
    assert _total(read_page.text) + _total(unread_page.text) == _total(all_page.text)
    assert "阅读筛选记录 00" in read_page.text
    assert "阅读筛选记录 03" not in read_page.text
    assert "阅读筛选记录 00" not in unread_page.text
    assert "阅读筛选记录 03" in unread_page.text


def test_false_is_a_real_repository_filter(
    database: Database,
    read_data: tuple[Source, Source, list[int]],
) -> None:
    with RepositoryUnitOfWork(database) as uow:
        rows, total = uow.items.paginate_with_sources(
            ItemQuery(is_read=False, source_scope=SourceScope.ALL, per_page=100)
        )

    assert total == 22
    assert rows
    assert all(item.is_read is False for item, *_source_fields in rows)


def test_read_filter_combines_with_category_and_source(
    read_client: TestClient,
    read_data: tuple[Source, Source, list[int]],
) -> None:
    first, second, _ids = read_data
    category = read_client.get(
        "/",
        params={
            "is_read": "yes",
            "category": Category.MODEL_TECHNOLOGY.value,
            "source_scope": "all",
        },
    )
    source = read_client.get(
        "/",
        params={"is_read": "no", "source_id": second.id, "source_scope": "all"},
    )

    assert _total(category.text) == 2
    assert "阅读筛选记录 00" in category.text
    assert "阅读筛选记录 01" not in category.text
    assert _total(source.text) == 11
    assert "阅读筛选记录 03" in source.text
    assert "阅读筛选记录 04" not in source.text
    assert first.id != second.id


def test_read_filter_pagination_clear_and_both_views_use_one_result_set(
    read_client: TestClient,
    read_data: tuple[Source, Source, list[int]],
) -> None:
    page = read_client.get(
        "/", params={"is_read": "no", "source_scope": "all", "per_page": 20}
    )

    assert _total(page.text) == 22
    assert re.search(r'href="[^"]*is_read=no[^"]*page=2', page.text.replace("&amp;", "&"))
    assert '<a class="btn btn-secondary" href="/" id="btn-clear-filter">清除</a>' in page.text
    card = page.text.split('id="list-a"', 1)[1].split('id="list-b"', 1)[0]
    compact = page.text.split('id="list-b"', 1)[1]
    visible_titles = set(re.findall(r"阅读筛选记录 \d{2}", card))
    assert visible_titles
    assert visible_titles == set(re.findall(r"阅读筛选记录 \d{2}", compact))


def test_single_and_batch_read_updates_change_filtered_counts_immediately(
    read_client: TestClient,
    read_data: tuple[Source, Source, list[int]],
) -> None:
    _first, _second, ids = read_data
    single = read_client.post(
        f"/items/{ids[3]}/read",
        data={"is_read": "true", "return_to": "/?is_read=no&source_scope=all"},
        follow_redirects=False,
    )
    after_single_read = read_client.get(
        "/", params={"is_read": "yes", "source_scope": "all"}
    )
    after_single_unread = read_client.get(
        "/", params={"is_read": "no", "source_scope": "all", "per_page": 100}
    )

    assert single.status_code == 303
    assert (_total(after_single_read.text), _total(after_single_unread.text)) == (4, 21)

    batch = read_client.post(
        "/items/batch-read",
        data={
            "item_ids": f"{ids[4]},{ids[5]}",
            "is_read": "true",
            "return_to": "/?is_read=no&source_scope=all",
        },
        follow_redirects=False,
    )
    after_batch_read = read_client.get(
        "/", params={"is_read": "yes", "source_scope": "all"}
    )
    after_batch_unread = read_client.get(
        "/", params={"is_read": "no", "source_scope": "all", "per_page": 100}
    )

    assert batch.status_code == 303
    assert (_total(after_batch_read.text), _total(after_batch_unread.text)) == (6, 19)


def test_runs_filter_has_independent_unique_controls(read_client: TestClient) -> None:
    response = read_client.get("/runs")

    assert response.status_code == 200
    assert 'class="filter-main runs-filter-grid"' in response.text
    assert 'class="field runs-date-field"' in response.text
    assert "date-pair" not in response.text
    for control_id, name in (
        ("rf-status", "status"),
        ("rf-trigger", "trigger"),
        ("rf-started-from", "started_from"),
        ("rf-started-to", "started_to"),
        ("rf-per-page", "per_page"),
    ):
        assert response.text.count(f'id="{control_id}"') == 1
        assert response.text.count(f'name="{name}"') == 1
    assert '<label for="rf-started-from">开始时间</label>' in response.text
    assert '<label for="rf-started-to">结束时间</label>' in response.text


def test_runs_filter_has_explicit_six_three_one_column_css() -> None:
    css = (PROJECT_ROOT / "app/web/static/styles.css").read_text(encoding="utf-8")

    base = css.split(".filter-main.runs-filter-grid {", 1)[1].split("}", 1)[0]
    medium = css.split("@media (max-width: 1200px)", 1)[1]
    small = css.split("@media (max-width: 768px)", 1)[1]
    assert base.count("minmax(") == 5
    assert "auto;" in base
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in medium
    assert "grid-template-columns: minmax(0, 1fr);" in small
    assert "justify-self: end;" in css
    assert "min-width: 0;" in css
    assert Path(PROJECT_ROOT / "app/web/templates/runs.html").is_file()


def test_expanded_run_details_keep_sources_reasons_and_summary_in_scoped_regions(
    read_client: TestClient,
    database: Database,
) -> None:
    now = datetime(2026, 7, 23, 4, 0, tzinfo=UTC)
    long_rule = "规则-" + "超长规则名称" * 30
    long_error = "https://errors.example/" + "very-long-segment-" * 40
    with RepositoryUnitOfWork(database) as uow:
        run = uow.crawl_runs.add(
            CrawlRun(
                started_at=now,
                finished_at=now + timedelta(seconds=8),
                status=CrawlStatus.PARTIAL_SUCCESS,
                trigger=RunTrigger.MANUAL_WEB,
                source_total=18,
                source_success=17,
                source_failed=1,
                rejection_reason_counts={long_rule: 7, "quality.below_minimum": 3},
                failure_reason_counts={"parse_or_collection.failed": 2, long_error: 1},
                error_summary=long_error,
            )
        )
        for index in range(18):
            source = uow.sources.add(
                Source(
                    name=f"来源-{index}-" + "超长来源名称" * 12,
                    source_type=SourceType.RSS,
                    start_url=f"https://source-{index}.example/" + "path-" * 30,
                    collector_name="rss",
                    collector_config={},
                    origin=SourceOrigin.PRESET,
                )
            )
            uow.crawl_source_executions.add(
                CrawlSourceExecution(
                    crawl_run_id=run.id,
                    source_id=source.id,
                    status="failed" if index == 17 else "success",
                    discovered_count=3,
                    accepted_count=2,
                    rejected_count=1,
                    new_count=1,
                    error=long_error if index == 17 else None,
                )
            )
        empty_run = uow.crawl_runs.add(
            CrawlRun(
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(hours=1) + timedelta(seconds=2),
                status=CrawlStatus.SUCCESS,
                trigger=RunTrigger.SCHEDULED,
                source_total=0,
                source_success=0,
                source_failed=0,
            )
        )

    response = read_client.get("/runs")
    assert response.status_code == 200
    soup = BeautifulSoup(response.text, "html.parser")
    detail = soup.select_one(f"#run-{run.id} + tr.tr-expand")
    assert detail is not None
    grid = detail.select_one(".expand-grid")
    assert grid is not None
    assert len(grid.select(":scope > .expand-main")) == 1
    assert len(grid.select(":scope > .expand-summary")) == 1
    assert len(grid.select(".expand-reasons > .expand-block")) == 2
    assert len(grid.select(".mini-table-scroll .mini-table tbody tr")) == 18
    assert long_rule in detail.get_text()
    assert long_error in detail.get_text()

    empty_detail = soup.select_one(f"#run-{empty_run.id} + tr.tr-expand")
    assert empty_detail is not None
    assert "本次没有未通过准入的原因" in empty_detail.get_text()
    assert "本次没有处理失败原因" in empty_detail.get_text()


def test_expanded_run_css_contains_overflow_and_responsive_guards() -> None:
    css = (PROJECT_ROOT / "app/web/static/styles.css").read_text(encoding="utf-8")

    assert "grid-template-columns: minmax(0, 1fr) minmax(220px, 280px);" in css
    assert ".expand-main,\n.expand-block,\n.expand-summary {\n  min-width: 0;" in css
    assert ".mini-table-scroll" in css
    assert "overflow-x: auto;" in css
    assert "overflow-wrap: anywhere;" in css
    assert "word-break: break-word;" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    small = css.split("@media (max-width: 768px)", 1)[1]
    assert ".expand-reasons" in small
    assert "grid-template-columns: minmax(0, 1fr);" in small
