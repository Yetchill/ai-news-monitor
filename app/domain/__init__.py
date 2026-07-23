"""Domain models and shared enumerations."""

from app.domain.classification import ClassificationResult, Classifier
from app.domain.collection import CollectContext, CollectedItem, Collector, Fetcher, FetchResult
from app.domain.enums import Category, CrawlStatus, SourceOrigin, SourceType
from app.domain.exports import ExportFormat, ExportQuery, ExportResult
from app.domain.models import (
    Base,
    CrawlRun,
    IntelligenceItem,
    ItemRevision,
    ScheduleSettings,
    Source,
)
from app.domain.queries import ItemFilter, ItemQuery, Page
from app.domain.update import SourceUpdateResult, SourceUpdateStatus, UpdateMode, UpdateResult

__all__ = [
    "Base",
    "Category",
    "ClassificationResult",
    "Classifier",
    "CollectContext",
    "CollectedItem",
    "Collector",
    "CrawlRun",
    "CrawlStatus",
    "ExportFormat",
    "ExportQuery",
    "ExportResult",
    "FetchResult",
    "Fetcher",
    "IntelligenceItem",
    "ItemFilter",
    "ItemQuery",
    "ItemRevision",
    "Page",
    "ScheduleSettings",
    "Source",
    "SourceOrigin",
    "SourceType",
    "SourceUpdateResult",
    "SourceUpdateStatus",
    "UpdateMode",
    "UpdateResult",
]
