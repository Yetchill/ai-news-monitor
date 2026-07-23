"""Backup-gated, exact-identifier purge for the four stage-eight-B retired sources."""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url

from app.config.settings import PROJECT_ROOT
from app.domain.models import IntelligenceItem
from app.services.item_normalization import INTERNAL_DISCOVERIES_KEY
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.utils.url import canonicalize_url

UnitOfWorkFactory = Callable[[], RepositoryUnitOfWork]
FORMAL_DATABASE_PATH = (PROJECT_ROOT / "data" / "intelligence.db").resolve()

RETIRED_IDENTIFIERS: Mapping[str, frozenset[str]] = {
    "openai-news-rss": frozenset(
        {
            "https://openai.com/news/rss.xml",
            "https://openai.com/blog/rss.xml",
        }
    ),
    "google-blog-rss": frozenset(
        {
            "https://blog.google/rss",
            "https://blog.google/rss/",
            "https://googleblog.blogspot.com/feeds/posts/default",
        }
    ),
    "qwen-agent-releases": frozenset(
        {
            "https://github.com/QwenLM/Qwen-Agent/releases",
            "https://github.com/QwenLM/Qwen-Agent",
        }
    ),
    "baidu-cloud-customer-cases": frozenset({"https://cloud.baidu.com/case/index.html"}),
}


class RetiredSourcePurgeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetiredSourceImpact:
    source_id: int
    slug: str | None
    name: str
    start_url: str
    item_count: int
    revision_count: int
    review_event_count: int
    crawl_execution_count: int
    discovery_reference_count: int

    @property
    def related_record_count(self) -> int:
        return self.revision_count + self.review_event_count + self.discovery_reference_count


@dataclass(frozen=True, slots=True)
class RetiredSourcePurgeResult:
    dry_run: bool
    impacts: tuple[RetiredSourceImpact, ...]
    deleted_sources: int = 0
    deleted_items: int = 0
    deleted_executions: int = 0
    deleted_empty_runs: int = 0
    cleaned_discovery_references: int = 0
    backup_path: Path | None = None


class RetiredSourcePurgeService:
    def __init__(self, database: Database, uow_factory: UnitOfWorkFactory) -> None:
        self._database = database
        self._uow_factory = uow_factory

    def plan(self) -> RetiredSourcePurgeResult:
        with self._uow_factory() as uow:
            sources = [
                source
                for source in uow.sources.list()
                if _is_retired(source.slug, source.start_url)
            ]
            impacts = tuple(
                RetiredSourceImpact(
                    source_id=source.id,
                    slug=source.slug,
                    name=source.name,
                    start_url=source.start_url,
                    item_count=uow.items.count_by_source(source.id),
                    revision_count=uow.revisions.count_by_source(source.id),
                    review_event_count=uow.review_events.count_by_source(source.id),
                    crawl_execution_count=uow.crawl_source_executions.count_by_source(source.id),
                    discovery_reference_count=_discovery_reference_count(
                        uow.items.list(), source.id
                    ),
                )
                for source in sources
            )
        return RetiredSourcePurgeResult(True, impacts)

    def purge(self, *, confirm: bool, backup_path: Path | None) -> RetiredSourcePurgeResult:
        if not confirm:
            raise RetiredSourcePurgeError("purge requires explicit confirm")
        source_path = _sqlite_path(self._database)
        if source_path.resolve() == FORMAL_DATABASE_PATH:
            raise RetiredSourcePurgeError(
                "refusing to purge the formal data/intelligence.db; use a verified copy"
            )
        if backup_path is None:
            raise RetiredSourcePurgeError("confirm requires an explicit backup path")
        backup = backup_path.expanduser().resolve()
        if backup == source_path.resolve():
            raise RetiredSourcePurgeError("backup path must differ from database path")
        if backup.exists():
            raise RetiredSourcePurgeError("backup path already exists")
        backup.parent.mkdir(parents=True, exist_ok=True)
        self._database.engine.dispose()
        _backup_sqlite(source_path, backup)

        plan = self.plan()
        deleted_sources = deleted_items = deleted_executions = cleaned = 0
        affected_runs: set[int] = set()
        with self._uow_factory() as uow:
            for impact in plan.impacts:
                source = uow.sources.get(impact.source_id)
                if source is None or not _is_retired(source.slug, source.start_url):
                    continue
                affected_runs.update(uow.crawl_source_executions.run_ids_by_source(source.id))
                deleted_executions += uow.crawl_source_executions.delete_by_source(source.id)
                cleaned += _remove_discovery_references(uow.items.list(), source.id)
                deleted_items += uow.items.delete_by_source(source.id)
                deleted_sources += uow.sources.delete(source.id)
            deleted_empty_runs = uow.crawl_runs.delete_empty_ids(affected_runs)
        return RetiredSourcePurgeResult(
            dry_run=False,
            impacts=plan.impacts,
            deleted_sources=deleted_sources,
            deleted_items=deleted_items,
            deleted_executions=deleted_executions,
            deleted_empty_runs=deleted_empty_runs,
            cleaned_discovery_references=cleaned,
            backup_path=backup,
        )


def _is_retired(slug: str | None, start_url: str) -> bool:
    if slug in RETIRED_IDENTIFIERS:
        return True
    normalized = canonicalize_url(start_url) or start_url
    return any(
        normalized == (canonicalize_url(url) or url)
        for urls in RETIRED_IDENTIFIERS.values()
        for url in urls
    )


def _discovery_reference_count(items: Sequence[IntelligenceItem], source_id: int) -> int:
    total = 0
    for item in items:
        extra = item.extra
        discoveries = extra.get(INTERNAL_DISCOVERIES_KEY)
        if not isinstance(discoveries, list):
            continue
        typed_discoveries = cast(list[object], discoveries)
        total += sum(_is_source_discovery(value, source_id) for value in typed_discoveries)
    return total


def _remove_discovery_references(items: Sequence[IntelligenceItem], source_id: int) -> int:
    removed = 0
    for item in items:
        extra = item.extra
        discoveries = extra.get(INTERNAL_DISCOVERIES_KEY)
        if not isinstance(discoveries, list):
            continue
        typed_discoveries = cast(list[object], discoveries)
        cleaned: list[object] = [
            value for value in typed_discoveries if not _is_source_discovery(value, source_id)
        ]
        difference = len(typed_discoveries) - len(cleaned)
        if difference:
            updated = dict(extra)
            if cleaned:
                updated[INTERNAL_DISCOVERIES_KEY] = cleaned
            else:
                updated.pop(INTERNAL_DISCOVERIES_KEY, None)
            item.extra = updated
            removed += difference
    return removed


def _is_source_discovery(value: object, source_id: int) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return mapping.get("source_id") == source_id


def _sqlite_path(database: Database) -> Path:
    url = make_url(database.database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise RetiredSourcePurgeError("retired-source purge supports file-backed SQLite only")
    return Path(url.database).expanduser().resolve()


def _backup_sqlite(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as original, sqlite3.connect(target) as backup:
        original.backup(backup)
