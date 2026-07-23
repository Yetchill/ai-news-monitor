"""Opt-in real-network smoke test for the complete stage-four pipeline."""

from pathlib import Path

import pytest

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.enums import CrawlStatus
from app.fetchers.http import HttpFetcher
from app.services.classification_service import ClassificationService
from app.services.crawl_service import CrawlService
from app.services.source_seed_service import SourceSeedService
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork

pytestmark = pytest.mark.network


@pytest.mark.asyncio
async def test_formal_sources_complete_pipeline_in_temporary_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "live-pipeline.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()

    try:

        def uow_factory() -> RepositoryUnitOfWork:
            return RepositoryUnitOfWork(database)

        SourceSeedService(uow_factory).seed()
        selected = {
            "国家数据局新闻动态",
            "中国互联网协会通知公告",
            "百度智能云新闻",
        }
        with uow_factory() as uow:
            for source in uow.sources.list():
                source.enabled = source.name in selected
                source.collector_config = {**source.collector_config, "max_items": 10}

        classification = ClassificationService(RuleBasedClassifier.from_yaml(), uow_factory)
        async with HttpFetcher(timeout_seconds=30, request_interval_seconds=0) as fetcher:
            pipeline = UpdatePipeline(
                uow_factory=uow_factory,
                crawl_service=CrawlService(default_collector_registry(), fetcher),
                classification_service=classification,
            )
            first = await pipeline.update()
            with RepositoryUnitOfWork(database) as uow:
                first_items = uow.items.list()
            first_ids_by_url = {item.canonical_url: item.id for item in first_items}
            second = await pipeline.update()
            with RepositoryUnitOfWork(database) as uow:
                second_items = uow.items.list()
                runs = uow.crawl_runs.list()
            second_ids_by_url = {item.canonical_url: item.id for item in second_items}

        assert first.status in {CrawlStatus.SUCCESS, CrawlStatus.PARTIAL_SUCCESS}
        assert second.status in {CrawlStatus.SUCCESS, CrawlStatus.PARTIAL_SUCCESS}
        assert first.source_success >= 1
        assert second.source_success >= 1
        assert first_items
        assert all(item.title and item.canonical_url and item.source_id for item in first_items)
        assert all(item.category is not None for item in first_items)
        assert len(first_items) == len(first_ids_by_url)
        assert len(second_items) == len(second_ids_by_url)
        assert first_ids_by_url.keys() <= second_ids_by_url.keys()
        assert all(
            second_ids_by_url[canonical_url] == item_id
            for canonical_url, item_id in first_ids_by_url.items()
        )
        assert second.new_count == len(second_ids_by_url.keys() - first_ids_by_url.keys())
        assert len(runs) == 2
        assert all(
            run.finished_at is not None and run.status is not CrawlStatus.RUNNING for run in runs
        )
        assert database_path != Path("data/intelligence.db")
    finally:
        database.dispose()
