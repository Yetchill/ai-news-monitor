"""Shared construction for the update pipeline used by CLI and local Web UI."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.exporters import ExcelExporter, WordExporter
from app.fetchers.http import HttpFetcher
from app.services.classification_service import ClassificationService
from app.services.crawl_service import CrawlService
from app.services.export_service import ExportService
from app.services.source_url_security import SafeHttpFetcher, SourceUrlGuard
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


def build_export_service(database: Database) -> ExportService:
    """Build the same export application service for CLI and Web adapters."""

    return ExportService(
        lambda: RepositoryUnitOfWork(database),
        (ExcelExporter(), WordExporter()),
    )


@asynccontextmanager
async def update_pipeline_context(
    database: Database,
    pipeline_class: type[UpdatePipeline] = UpdatePipeline,
) -> AsyncGenerator[UpdatePipeline]:
    """Build one pipeline execution with a safely closed HTTP client."""

    def uow_factory() -> RepositoryUnitOfWork:
        return RepositoryUnitOfWork(database)

    classification = ClassificationService(RuleBasedClassifier.from_yaml(), uow_factory)
    async with HttpFetcher() as fetcher, SafeHttpFetcher(SourceUrlGuard()) as user_source_fetcher:
        yield pipeline_class(
            uow_factory=uow_factory,
            crawl_service=CrawlService(
                default_collector_registry(),
                fetcher,
                user_source_fetcher=user_source_fetcher,
            ),
            classification_service=classification,
        )
