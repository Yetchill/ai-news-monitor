"""Repositories and a unit-of-work transaction boundary."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, cast

from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.domain.enums import (
    CrawlStatus,
    LifecycleState,
    PrimaryType,
    ReviewStatus,
    RunTrigger,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceRole,
    SourceScope,
    VerificationStatus,
)
from app.domain.models import (
    Base,
    CrawlRun,
    CrawlSourceExecution,
    IntelligenceItem,
    ItemReviewEvent,
    ItemRevision,
    ScheduleSettings,
    Source,
)
from app.domain.queries import ItemFilter, ItemQuery
from app.storage.database import Database


class RepositoryError(ValueError):
    """Raised when a repository operation is invalid."""


class BaseRepository[ModelT: Base]:
    """Small, typed CRUD surface shared by model-specific repositories."""

    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entity: ModelT) -> ModelT:
        self._session.add(entity)
        self._session.flush()
        return entity

    def get(self, entity_id: int) -> ModelT | None:
        return self._session.get(self.model, entity_id)

    def list(self) -> list[ModelT]:
        statement = select(self.model).order_by(self.model.id)  # type: ignore[attr-defined]
        return list(self._session.scalars(statement))

    def update(self, entity_id: int, changes: Mapping[str, Any]) -> ModelT | None:
        entity = self.get(entity_id)
        if entity is None:
            return None
        valid_fields = set(self.model.__table__.columns.keys()) - {"id"}
        unknown_fields = set(changes) - valid_fields
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise RepositoryError(f"Unknown or immutable fields: {fields}")
        for field, value in changes.items():
            setattr(entity, field, value)
        self._session.flush()
        return entity

    def delete(self, entity_id: int) -> bool:
        entity = self.get(entity_id)
        if entity is None:
            return False
        self._session.delete(entity)
        self._session.flush()
        return True


class SourceRepository(BaseRepository[Source]):
    model = Source

    def get_by_start_url(self, start_url: str) -> Source | None:
        return self._session.scalar(select(Source).where(Source.start_url == start_url))

    def get_by_slug(self, slug: str) -> Source | None:
        return self._session.scalar(select(Source).where(Source.slug == slug))

    def list_enabled(self) -> list[Source]:
        statement = select(Source).where(Source.enabled.is_(True)).order_by(Source.id)
        return list(self._session.scalars(statement))

    def list_enabled_formal(self) -> list[Source]:
        statement = (
            select(Source)
            .where(Source.enabled.is_(True), Source.source_kind == SourceKind.FORMAL)
            .order_by(Source.id)
        )
        return list(self._session.scalars(statement))

    def list_active(self) -> list[Source]:
        statement = (
            select(Source)
            .where(
                Source.enabled.is_(True),
                Source.lifecycle_state == LifecycleState.ACTIVE,
            )
            .order_by(Source.id)
        )
        return list(self._session.scalars(statement))

    def list_options(self) -> list[tuple[int, str, SourceKind, bool, LifecycleState]]:
        statement = select(
            Source.id, Source.name, Source.source_kind, Source.enabled, Source.lifecycle_state
        ).where(
            Source.catalog_managed.is_(True),
            Source.lifecycle_state == LifecycleState.ACTIVE,
        ).order_by(Source.name, Source.id)
        return [
            (source_id, name, source_kind, enabled, lifecycle_state)
            for source_id, name, source_kind, enabled, lifecycle_state in self._session.execute(
                statement
            )
        ]

    def paginate(
        self,
        *,
        page: int,
        per_page: int,
        catalog_filter: str = "all",
        search: str | None = None,
    ) -> tuple[list[Source], int]:
        filters = _source_catalog_filters(catalog_filter)
        if search:
            escaped = _escape_like(search)
            pattern = f"%{escaped}%"
            filters.append(
                or_(
                    Source.name.ilike(pattern, escape="\\"),
                    Source.start_url.ilike(pattern, escape="\\"),
                )
            )
        total = self._session.scalar(select(func.count(Source.id)).where(*filters)) or 0
        statement = (
            select(Source)
            .where(*filters)
            .order_by(Source.name, Source.id)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return list(self._session.scalars(statement)), total

    def overview(self) -> tuple[int, int, int, int]:
        statement = select(
            func.count(Source.id),
            func.count(Source.id).filter(
                Source.enabled.is_(True), Source.lifecycle_state == LifecycleState.ACTIVE
            ),
            func.count(Source.id).filter(
                or_(Source.last_error.is_not(None), Source.enabled.is_(False))
            ),
            func.count(Source.id).filter(Source.lifecycle_state == LifecycleState.CANDIDATE),
        )
        row = self._session.execute(statement.where(_visible_source_filter())).one()
        return tuple(int(value or 0) for value in row)  # type: ignore[return-value]


class IntelligenceItemRepository(BaseRepository[IntelligenceItem]):
    model = IntelligenceItem

    def get_by_canonical_url(self, canonical_url: str) -> IntelligenceItem | None:
        return self._session.scalar(
            select(IntelligenceItem).where(IntelligenceItem.canonical_url == canonical_url)
        )

    def list_by_source(self, source_id: int) -> list[IntelligenceItem]:
        statement = (
            select(IntelligenceItem)
            .where(IntelligenceItem.source_id == source_id)
            .order_by(IntelligenceItem.id)
        )
        return list(self._session.scalars(statement))

    def list_recent_by_source(self, source_id: int, *, limit: int = 5) -> list[IntelligenceItem]:
        statement = (
            select(IntelligenceItem)
            .where(
                IntelligenceItem.source_id == source_id,
                IntelligenceItem.is_active.is_(True),
            )
            .order_by(*_item_order())
            .limit(max(1, min(limit, 20)))
        )
        return list(self._session.scalars(statement))

    def overview(
        self,
        *,
        today_start: datetime,
        today_end: datetime,
        source_scope: SourceScope,
    ) -> tuple[int, int, int, int]:
        filters = _item_filters(ItemFilter(source_scope=source_scope))
        is_unclassified = or_(
            IntelligenceItem.manual_category == "unclassified",
            and_(
                IntelligenceItem.manual_category.is_(None),
                IntelligenceItem.category == "unclassified",
            ),
        )
        statement = (
            select(
                func.count(IntelligenceItem.id),
                func.count(IntelligenceItem.id).filter(IntelligenceItem.is_read.is_(False)),
                func.count(IntelligenceItem.id).filter(
                    IntelligenceItem.discovered_at >= today_start,
                    IntelligenceItem.discovered_at < today_end,
                ),
                func.count(IntelligenceItem.id).filter(is_unclassified),
            )
            .join(Source, IntelligenceItem.source_id == Source.id)
            .where(*filters)
        )
        row = self._session.execute(statement).one()
        return tuple(int(value or 0) for value in row)  # type: ignore[return-value]

    def ai_work_overview(self) -> tuple[int, int]:
        is_unclassified = or_(
            IntelligenceItem.manual_category == "unclassified",
            and_(
                IntelligenceItem.manual_category.is_(None),
                IntelligenceItem.category == "unclassified",
            ),
        )
        statement = select(
            func.count(IntelligenceItem.id).filter(is_unclassified),
            func.count(IntelligenceItem.id).filter(
                or_(
                    IntelligenceItem.ai_summary.is_(None),
                    IntelligenceItem.ai_summary == "",
                )
            ),
        ).where(IntelligenceItem.is_active.is_(True))
        row = self._session.execute(statement).one()
        return int(row[0] or 0), int(row[1] or 0)

    def count_by_source(self, source_id: int) -> int:
        return (
            self._session.scalar(
                select(func.count(IntelligenceItem.id)).where(
                    IntelligenceItem.source_id == source_id
                )
            )
            or 0
        )

    def list_children(self, parent_item_id: int) -> list[IntelligenceItem]:
        statement = (
            select(IntelligenceItem)
            .where(IntelligenceItem.parent_item_id == parent_item_id)
            .order_by(IntelligenceItem.id)
        )
        return list(self._session.scalars(statement))

    def delete_by_source(self, source_id: int) -> int:
        result = self._session.execute(
            delete(IntelligenceItem).where(IntelligenceItem.source_id == source_id)
        )
        return _affected_rows(result)

    def get_by_source_fingerprint(
        self,
        source_id: int,
        fingerprint: str,
    ) -> IntelligenceItem | None:
        return self._session.scalar(
            select(IntelligenceItem).where(
                IntelligenceItem.source_id == source_id,
                IntelligenceItem.fingerprint == fingerprint,
            )
        )

    def add_or_get_existing(
        self,
        entity: IntelligenceItem,
    ) -> tuple[IntelligenceItem, bool]:
        """Insert under a savepoint and recover a concurrently inserted duplicate."""

        try:
            with self._session.begin_nested():
                self._session.add(entity)
                self._session.flush()
            return entity, True
        except IntegrityError:
            existing = self.get_by_canonical_url(entity.canonical_url)
            if existing is None:
                existing = self.get_by_source_fingerprint(entity.source_id, entity.fingerprint)
            if existing is None:
                raise
            return existing, False

    def paginate_with_sources(
        self, query: ItemQuery
    ) -> tuple[list[tuple[IntelligenceItem, str, SourceKind, SourceRole]], int]:
        total = self.count_filtered(query)
        rows = self.list_filtered_with_sources(
            query,
            limit=query.per_page,
            offset=(query.page - 1) * query.per_page,
        )
        return rows, total

    def count_filtered(self, item_filter: ItemFilter) -> int:
        filters = _item_filters(item_filter)
        total_statement = (
            select(func.count(IntelligenceItem.id))
            .join(Source, IntelligenceItem.source_id == Source.id)
            .where(*filters)
        )
        return self._session.scalar(total_statement) or 0

    def list_filtered_with_sources(
        self,
        item_filter: ItemFilter,
        *,
        limit: int,
        offset: int = 0,
    ) -> list[tuple[IntelligenceItem, str, SourceKind, SourceRole]]:
        filters = _item_filters(item_filter)
        statement = (
            select(IntelligenceItem, Source.name, Source.source_kind, Source.source_role)
            .join(Source, IntelligenceItem.source_id == Source.id)
            .where(*filters)
            .order_by(*_item_order())
            .offset(offset)
            .limit(limit)
        )
        return [
            (item, source_name, source_kind, source_role)
            for item, source_name, source_kind, source_role in self._session.execute(statement)
        ]


class CrawlRunRepository(BaseRepository[CrawlRun]):
    model = CrawlRun

    def list_recent(self, limit: int = 5) -> list[CrawlRun]:
        statement = (
            select(CrawlRun)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(max(1, min(limit, 100)))
        )
        return list(self._session.scalars(statement))

    def paginate(
        self,
        *,
        page: int,
        per_page: int,
        status: CrawlStatus | None = None,
        trigger: RunTrigger | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
    ) -> tuple[list[CrawlRun], int]:
        filters = _crawl_run_filters(
            status=status,
            trigger=trigger,
            started_from=started_from,
            started_to=started_to,
        )
        total = self._session.scalar(select(func.count(CrawlRun.id)).where(*filters)) or 0
        statement = (
            select(CrawlRun)
            .where(*filters)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        return list(self._session.scalars(statement)), total

    def overview(self, *, since: datetime) -> tuple[int, int, int, int]:
        statement = select(
            func.count(CrawlRun.id),
            func.count(CrawlRun.id).filter(
                CrawlRun.started_at >= since, CrawlRun.status == CrawlStatus.SUCCESS
            ),
            func.count(CrawlRun.id).filter(
                CrawlRun.started_at >= since, CrawlRun.status == CrawlStatus.PARTIAL_SUCCESS
            ),
            func.count(CrawlRun.id).filter(
                CrawlRun.started_at >= since, CrawlRun.status == CrawlStatus.FAILED
            ),
        )
        row = self._session.execute(statement).one()
        return tuple(int(value or 0) for value in row)  # type: ignore[return-value]

    def delete_empty(self) -> int:
        has_execution = exists(
            select(CrawlSourceExecution.id).where(CrawlSourceExecution.crawl_run_id == CrawlRun.id)
        )
        result = self._session.execute(delete(CrawlRun).where(~has_execution))
        return _affected_rows(result)

    def delete_empty_ids(self, run_ids: set[int]) -> int:
        if not run_ids:
            return 0
        has_execution = exists(
            select(CrawlSourceExecution.id).where(CrawlSourceExecution.crawl_run_id == CrawlRun.id)
        )
        result = self._session.execute(
            delete(CrawlRun).where(CrawlRun.id.in_(run_ids), ~has_execution)
        )
        return _affected_rows(result)


class ItemRevisionRepository(BaseRepository[ItemRevision]):
    model = ItemRevision

    def list_by_item(self, item_id: int) -> list[ItemRevision]:
        statement = (
            select(ItemRevision)
            .where(ItemRevision.item_id == item_id)
            .order_by(ItemRevision.changed_at, ItemRevision.id)
        )
        return list(self._session.scalars(statement))

    def count_by_source(self, source_id: int) -> int:
        return (
            self._session.scalar(
                select(func.count(ItemRevision.id))
                .join(IntelligenceItem, ItemRevision.item_id == IntelligenceItem.id)
                .where(IntelligenceItem.source_id == source_id)
            )
            or 0
        )


class CrawlSourceExecutionRepository(BaseRepository[CrawlSourceExecution]):
    model = CrawlSourceExecution

    def get_by_run_source(self, crawl_run_id: int, source_id: int) -> CrawlSourceExecution | None:
        return self._session.scalar(
            select(CrawlSourceExecution).where(
                CrawlSourceExecution.crawl_run_id == crawl_run_id,
                CrawlSourceExecution.source_id == source_id,
            )
        )

    def list_for_runs(
        self, run_ids: Sequence[int]
    ) -> list[tuple[CrawlSourceExecution, str]]:
        if not run_ids:
            return []
        statement = (
            select(CrawlSourceExecution, Source.name)
            .join(Source, CrawlSourceExecution.source_id == Source.id)
            .where(CrawlSourceExecution.crawl_run_id.in_(run_ids))
            .order_by(CrawlSourceExecution.crawl_run_id.desc(), Source.name, Source.id)
        )
        return [
            (execution, source_name)
            for execution, source_name in self._session.execute(statement)
        ]

    def list_recent_by_source(
        self, source_id: int, *, limit: int = 5
    ) -> list[tuple[CrawlSourceExecution, CrawlRun]]:
        statement = (
            select(CrawlSourceExecution, CrawlRun)
            .join(CrawlRun, CrawlSourceExecution.crawl_run_id == CrawlRun.id)
            .where(CrawlSourceExecution.source_id == source_id)
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(max(1, min(limit, 20)))
        )
        return list(self._session.execute(statement).tuples())

    def period_stats(self, source_id: int, *, since: datetime) -> tuple[int, ...]:
        statement = (
            select(
                func.coalesce(func.sum(CrawlSourceExecution.discovered_count), 0),
                func.coalesce(func.sum(CrawlSourceExecution.accepted_count), 0),
                func.coalesce(func.sum(CrawlSourceExecution.rejected_count), 0),
                func.coalesce(func.sum(CrawlSourceExecution.new_count), 0),
                func.coalesce(func.sum(CrawlSourceExecution.updated_count), 0),
                func.coalesce(func.sum(CrawlSourceExecution.failed_count), 0),
                func.count(CrawlSourceExecution.id).filter(
                    CrawlSourceExecution.status == "success"
                ),
                func.count(CrawlSourceExecution.id).filter(
                    CrawlSourceExecution.status != "success"
                ),
            )
            .join(CrawlRun, CrawlSourceExecution.crawl_run_id == CrawlRun.id)
            .where(
                CrawlSourceExecution.source_id == source_id,
                CrawlRun.started_at >= since,
            )
        )
        row = self._session.execute(statement).one()
        return tuple(int(value or 0) for value in row)

    def count_by_source(self, source_id: int) -> int:
        return (
            self._session.scalar(
                select(func.count(CrawlSourceExecution.id)).where(
                    CrawlSourceExecution.source_id == source_id
                )
            )
            or 0
        )

    def run_ids_by_source(self, source_id: int) -> set[int]:
        return set(
            self._session.scalars(
                select(CrawlSourceExecution.crawl_run_id).where(
                    CrawlSourceExecution.source_id == source_id
                )
            )
        )

    def delete_by_source(self, source_id: int) -> int:
        result = self._session.execute(
            delete(CrawlSourceExecution).where(CrawlSourceExecution.source_id == source_id)
        )
        return _affected_rows(result)


class ItemReviewEventRepository(BaseRepository[ItemReviewEvent]):
    model = ItemReviewEvent

    def list_by_item(self, item_id: int) -> list[ItemReviewEvent]:
        statement = (
            select(ItemReviewEvent)
            .where(ItemReviewEvent.item_id == item_id)
            .order_by(ItemReviewEvent.changed_at, ItemReviewEvent.id)
        )
        return list(self._session.scalars(statement))

    def count_by_source(self, source_id: int) -> int:
        return (
            self._session.scalar(
                select(func.count(ItemReviewEvent.id))
                .join(IntelligenceItem, ItemReviewEvent.item_id == IntelligenceItem.id)
                .where(IntelligenceItem.source_id == source_id)
            )
            or 0
        )


class ScheduleSettingsRepository(BaseRepository[ScheduleSettings]):
    model = ScheduleSettings

    def get_singleton(self) -> ScheduleSettings | None:
        return self._session.get(ScheduleSettings, 1)

    def add_singleton_if_missing(self, entity: ScheduleSettings) -> ScheduleSettings:
        """Create the singleton without racing another first writer."""

        statement = (
            sqlite_insert(ScheduleSettings)
            .values(
                id=1,
                schedule_enabled=entity.schedule_enabled,
                schedule_hour=entity.schedule_hour,
                schedule_minute=entity.schedule_minute,
                schedule_days_mask=entity.schedule_days_mask,
                timezone=entity.timezone,
                updated_at=entity.updated_at,
                last_scheduled_trigger_at=entity.last_scheduled_trigger_at,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        self._session.execute(statement)
        row = self.get_singleton()
        if row is None:
            raise RuntimeError("could not create schedule settings singleton")
        return row


class RepositoryUnitOfWork:
    """Expose repositories while keeping SQLAlchemy sessions out of callers."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._session: Session | None = None
        self.sources: SourceRepository
        self.items: IntelligenceItemRepository
        self.crawl_runs: CrawlRunRepository
        self.revisions: ItemRevisionRepository
        self.crawl_source_executions: CrawlSourceExecutionRepository
        self.review_events: ItemReviewEventRepository
        self.schedule_settings: ScheduleSettingsRepository

    def __enter__(self) -> "RepositoryUnitOfWork":
        session = self._database.session_factory()
        self._session = session
        self.sources = SourceRepository(session)
        self.items = IntelligenceItemRepository(session)
        self.crawl_runs = CrawlRunRepository(session)
        self.revisions = ItemRevisionRepository(session)
        self.crawl_source_executions = CrawlSourceExecutionRepository(session)
        self.review_events = ItemReviewEventRepository(session)
        self.schedule_settings = ScheduleSettingsRepository(session)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._session is None:
            return
        try:
            if exception_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    def commit(self) -> None:
        """Commit current changes and immediately begin the next transaction."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        self._session.commit()

    def rollback(self) -> None:
        """Roll back current changes."""

        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        self._session.rollback()


def _item_filters(query: ItemFilter) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = [IntelligenceItem.is_active.is_(True)]
    if query.source_scope is SourceScope.LEADERSHIP:
        publication_ready = _publication_ready()
        filters.extend(
            (
                Source.enabled.is_(True),
                Source.lifecycle_state == LifecycleState.ACTIVE,
                Source.homepage_visible.is_(True),
                Source.audience.in_((SourceAudience.LEADERSHIP, SourceAudience.ALL)),
                IntelligenceItem.admission_accepted.is_(True),
                publication_ready,
            )
        )
    elif query.source_scope is SourceScope.FORMAL_EXPORT:
        publication_ready = _publication_ready()
        filters.extend(
            (
                Source.enabled.is_(True),
                Source.lifecycle_state == LifecycleState.ACTIVE,
                Source.export_visible.is_(True),
                IntelligenceItem.admission_accepted.is_(True),
                publication_ready,
            )
        )
    elif query.source_scope is SourceScope.NON_FORMAL:
        filters.append(Source.source_kind != SourceKind.FORMAL)
    elif query.source_scope is SourceScope.DISABLED:
        filters.append(Source.enabled.is_(False))
    elif query.source_scope is SourceScope.FALLBACK:
        filters.append(Source.source_kind == SourceKind.FALLBACK)
    elif query.source_scope is SourceScope.INDUSTRY_LEADS:
        filters.extend(
            (
                Source.lifecycle_state == LifecycleState.ACTIVE,
                or_(
                    IntelligenceItem.primary_type == PrimaryType.INDUSTRY_SIGNAL,
                    IntelligenceItem.verification_status.in_(
                        (
                            VerificationStatus.MEDIA_ONLY,
                            VerificationStatus.RUMOR_OR_PREDICTION,
                        )
                    ),
                    IntelligenceItem.review_status == ReviewStatus.PENDING,
                ),
            )
        )
    if query.keyword:
        escaped = _escape_like(query.keyword)
        pattern = f"%{escaped}%"
        filters.append(
            or_(
                IntelligenceItem.title.ilike(pattern, escape="\\"),
                IntelligenceItem.summary.ilike(pattern, escape="\\"),
            )
        )
    if query.category is not None:
        filters.append(
            or_(
                IntelligenceItem.manual_category == query.category,
                and_(
                    IntelligenceItem.manual_category.is_(None),
                    IntelligenceItem.category == query.category,
                ),
            )
        )
    if query.primary_type is not None:
        filters.append(
            or_(
                IntelligenceItem.manual_primary_type == query.primary_type,
                and_(
                    IntelligenceItem.manual_primary_type.is_(None),
                    IntelligenceItem.primary_type == query.primary_type,
                ),
            )
        )
    if query.verification_status is not None:
        filters.append(IntelligenceItem.verification_status == query.verification_status)
    if query.review_status is not None:
        filters.append(IntelligenceItem.review_status == query.review_status)
    if query.source_id is not None:
        filters.append(IntelligenceItem.source_id == query.source_id)
    if query.favorite is not None:
        filters.append(IntelligenceItem.is_favorite.is_(query.favorite))
    if query.is_read is not None:
        filters.append(IntelligenceItem.is_read.is_(query.is_read))
    if query.published_from is not None:
        filters.append(IntelligenceItem.published_at >= query.published_from)
    if query.published_to is not None:
        filters.append(IntelligenceItem.published_at < query.published_to)
    if query.discovered_from is not None:
        filters.append(IntelligenceItem.discovered_at >= query.discovered_from)
    if query.discovered_to is not None:
        filters.append(IntelligenceItem.discovered_at < query.discovered_to)
    if query.unclassified is not None:
        is_unclassified = or_(
            IntelligenceItem.manual_category == "unclassified",
            and_(
                IntelligenceItem.manual_category.is_(None),
                IntelligenceItem.category == "unclassified",
            ),
        )
        filters.append(is_unclassified if query.unclassified else ~is_unclassified)
    return filters


def _source_allows_primary_type() -> ColumnElement[bool]:
    values = func.json_each(Source.allowed_primary_types).table_valued("value")
    return or_(
        func.json_array_length(Source.allowed_primary_types) == 0,
        exists(
            select(1).select_from(values).where(values.c.value == IntelligenceItem.primary_type)
        ),
    )


def _publication_ready() -> ColumnElement[bool]:
    return or_(
        IntelligenceItem.taxonomy_version != "v2",
        and_(
            IntelligenceItem.verification_status.in_(
                (
                    VerificationStatus.OFFICIAL_CONFIRMED,
                    VerificationStatus.OFFICIAL_LINKED,
                    VerificationStatus.MULTI_SOURCE_CONFIRMED,
                )
            ),
            IntelligenceItem.review_status.in_((ReviewStatus.NOT_REQUIRED, ReviewStatus.APPROVED)),
            IntelligenceItem.primary_type.not_in(
                (PrimaryType.UNCLASSIFIED, PrimaryType.INDUSTRY_SIGNAL)
            ),
            _source_allows_primary_type(),
        ),
    )


def _item_order() -> tuple[ColumnElement[Any], ...]:
    effective_date = func.coalesce(
        IntelligenceItem.published_at,
        IntelligenceItem.discovered_at,
        IntelligenceItem.updated_at,
    )
    return effective_date.desc(), IntelligenceItem.id.desc()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _affected_rows(result: object) -> int:
    return max(0, cast(CursorResult[Any], result).rowcount or 0)


def _source_catalog_filters(value: str) -> list[ColumnElement[bool]]:
    if value == "monitoring":
        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState.ACTIVE,
        ]
    if value in {state.value for state in LifecycleState}:
        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState(value),
        ]
    if value == "official":
        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState.ACTIVE,
            Source.source_role.not_in((SourceRole.MEDIA_DISCOVERY, SourceRole.FALLBACK)),
        ]
    if value == "media":
        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState.ACTIVE,
            Source.source_role == SourceRole.MEDIA_DISCOVERY,
        ]
    if value == "blocked":
        from app.domain.enums import ImplementationStatus

        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState.ACTIVE,
            Source.implementation_status == ImplementationStatus.BLOCKED_BY_JAVASCRIPT,
        ]
    if value == "needs_custom":
        from app.domain.enums import ImplementationStatus

        return [
            _visible_source_filter(),
            Source.lifecycle_state == LifecycleState.ACTIVE,
            Source.implementation_status == ImplementationStatus.NEEDS_CUSTOM_COLLECTOR,
        ]
    return []


def _visible_source_filter() -> ColumnElement[bool]:
    """Hide reconciled legacy presets while retaining user-created sources."""

    return or_(
        Source.catalog_managed.is_(True),
        Source.origin != SourceOrigin.PRESET,
        Source.source_kind != SourceKind.FALLBACK,
    )


def _crawl_run_filters(
    *,
    status: CrawlStatus | None,
    trigger: RunTrigger | None,
    started_from: datetime | None,
    started_to: datetime | None,
) -> list[ColumnElement[bool]]:
    filters: list[ColumnElement[bool]] = []
    if status is not None:
        filters.append(CrawlRun.status == status)
    if trigger is not None:
        filters.append(CrawlRun.trigger == trigger)
    if started_from is not None:
        filters.append(CrawlRun.started_at >= started_from)
    if started_to is not None:
        filters.append(CrawlRun.started_at < started_to)
    return filters
