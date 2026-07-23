"""Stage-four update pipeline behavior with no public-network dependency."""

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest

from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import CollectorRegistry
from app.domain.collection import CollectContext, CollectedItem, Fetcher, FetchResult
from app.domain.enums import Category, CrawlStatus, RunTrigger, SourceKind, SourceOrigin, SourceType
from app.domain.models import IntelligenceItem, Source
from app.domain.update import SourceUpdateStatus, UpdateMode
from app.fetchers.errors import NetworkFetchError
from app.services.classification_service import ClassificationService
from app.services.crawl_run_service import CrawlRunService
from app.services.crawl_service import CrawlService
from app.services.item_persistence_service import ItemPersistenceService
from app.services.update_pipeline import SourceDisabledError, SourceNotFoundError, UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.utils.fingerprint import generate_item_fingerprint


class ScenarioFetcher:
    """Collector test backend carried through the registry's Fetcher slot."""

    def __init__(self) -> None:
        self.responses: dict[str, list[list[CollectedItem] | Exception]] = {}
        self.contexts: list[CollectContext] = []
        self.active_uow_count = 0

    async def fetch(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        del url, headers
        raise AssertionError("ScenarioCollector does not perform transport fetches")

    async def post(
        self,
        url: str,
        *,
        body: str,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        del url, body, headers
        raise AssertionError("ScenarioCollector does not perform transport POST")


class ScenarioCollector:
    name = "scenario"

    def __init__(self, fetcher: Fetcher) -> None:
        self.backend = cast(ScenarioFetcher, fetcher)

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        assert self.backend.active_uow_count == 0, "network collection held a database UoW"
        self.backend.contexts.append(context)
        response = self.backend.responses[context.source_url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BlockingScenarioFetcher(ScenarioFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()


class BlockingScenarioCollector:
    name = "blocking-scenario"

    def __init__(self, fetcher: Fetcher) -> None:
        self.backend = cast(BlockingScenarioFetcher, fetcher)

    async def collect(self, context: CollectContext) -> list[CollectedItem]:
        self.backend.contexts.append(context)
        self.backend.started.set()
        await self.backend.release.wait()
        return []


class TrackingUnitOfWork(RepositoryUnitOfWork):
    def __init__(self, database: Database, tracker: ScenarioFetcher) -> None:
        super().__init__(database)
        self._tracker = tracker

    def __enter__(self) -> "TrackingUnitOfWork":
        self._tracker.active_uow_count += 1
        try:
            super().__enter__()
        except Exception:
            self._tracker.active_uow_count -= 1
            raise
        return self

    def __exit__(self, *args: object) -> None:
        try:
            super().__exit__(*args)  # pyright: ignore[reportArgumentType]
        finally:
            self._tracker.active_uow_count -= 1


def _source(name: str, url: str, *, enabled: bool = True) -> Source:
    return Source(
        name=name,
        source_type=SourceType.CUSTOM,
        start_url=url,
        enabled=enabled,
        collector_name="scenario",
        collector_config={},
        origin=SourceOrigin.PRESET,
        minimum_quality_score=0,
        allow_external_links=True,
    )


def _item(
    url: str,
    *,
    title: str = "关于开展优秀人工智能案例征集的通知",
    summary: str | None = "欢迎企业提交材料",
    published_at: datetime | None = None,
    extra: Mapping[str, object] | None = None,
) -> CollectedItem:
    return CollectedItem(
        title=title,
        original_url=url,
        canonical_url=url,
        summary=summary,
        published_at=published_at,
        extra=extra or {},
    )


def _pipeline(
    database: Database,
    backend: ScenarioFetcher,
    *,
    crawl_run_service: CrawlRunService | None = None,
    uow_factory: Callable[[], RepositoryUnitOfWork] | None = None,
) -> UpdatePipeline:
    resolved_uow_factory = uow_factory
    if resolved_uow_factory is None:

        def default_uow_factory() -> RepositoryUnitOfWork:
            return TrackingUnitOfWork(database, backend)

        resolved_uow_factory = default_uow_factory

    registry = CollectorRegistry()
    registry.register("scenario", ScenarioCollector)
    classification = ClassificationService(RuleBasedClassifier.from_yaml(), resolved_uow_factory)
    return UpdatePipeline(
        uow_factory=resolved_uow_factory,
        crawl_service=CrawlService(registry, backend),
        classification_service=classification,
        persistence_service=ItemPersistenceService(resolved_uow_factory),
        crawl_run_service=crawl_run_service,
    )


def _add_sources(database: Database, *sources: Source) -> None:
    with RepositoryUnitOfWork(database) as uow:
        for source in sources:
            uow.sources.add(source)


@pytest.mark.asyncio
async def test_first_run_persists_classification_and_second_run_is_skipped(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    source.last_error = "previous transient failure"
    _add_sources(database, source)
    collected = _item("https://example.com/article/1?utm_source=test")
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[collected], [collected]]
    pipeline = _pipeline(database, backend)

    first = await pipeline.update()
    second = await pipeline.update()

    assert first.status is CrawlStatus.SUCCESS
    assert (first.discovered_count, first.new_count, first.updated_count) == (1, 1, 0)
    assert (second.new_count, second.updated_count, second.skipped_count) == (0, 0, 1)
    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        revisions = uow.revisions.list()
        stored_source = uow.sources.get(source.id)
    assert len(items) == 1
    assert items[0].canonical_url == "https://example.com/article/1"
    assert items[0].category is Category.SOLICITATION
    assert items[0].classification_score is not None
    assert items[0].classification_reason
    assert items[0].automatic_category_provider == "rule_based"
    assert revisions == []
    assert stored_source is not None
    assert stored_source.last_checked_at is not None
    assert stored_source.last_success_at is not None
    assert stored_source.last_error is None


@pytest.mark.asyncio
async def test_admission_rejection_is_audited_and_never_counted_as_new(
    database: Database,
) -> None:
    source = _source("正式来源", "https://example.com/feed")
    source.minimum_quality_score = 50
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [
            _item(
                "https://example.com/recruitment",
                title="人工智能企业校园招聘公告",
            ),
            _item(
                "https://example.com/release",
                title="新一代大模型正式发布",
            ),
        ]
    ]

    result = await _pipeline(database, backend).update()

    assert result.discovered_count == 2
    assert result.normalized_count == 2
    assert result.accepted_count == 1
    assert result.rejected_count == 1
    assert result.classified_count == 1
    assert result.new_count == 1
    assert result.rejection_reason_counts == {"content.recruitment": 1}
    with RepositoryUnitOfWork(database) as uow:
        stored_items = uow.items.list()
        assert [item.title for item in stored_items] == ["新一代大模型正式发布"]
        assert stored_items[0].admission_accepted is True
        run = uow.crawl_runs.get(result.crawl_run_id)
        assert run is not None
        assert run.rejection_reason_counts == {"content.recruitment": 1}


@pytest.mark.asyncio
async def test_same_batch_duplicate_item_has_one_mutually_exclusive_outcome(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    duplicate = _item("https://example.com/article?b=2&a=1&utm_source=test#fragment")
    equivalent = _item("https://example.com/article?a=1&b=2")
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[duplicate, equivalent]]

    result = await _pipeline(database, backend).update()

    assert result.discovered_count == 2
    assert (result.new_count, result.updated_count, result.skipped_count) == (1, 0, 0)
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.items.list()) == 1
        assert uow.revisions.list() == []


@pytest.mark.asyncio
async def test_final_normalization_preserves_configured_business_query_parameters(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    source.collector_config = {"keep_query_params": ["utm_source", "id"]}
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/article?ignored=x&utm_source=route&id=42#fragment")]
    ]

    result = await _pipeline(database, backend).update()

    assert result.new_count == 1
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.list()[0]
    assert stored.canonical_url == "https://example.com/article?id=42&utm_source=route"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second", "expected_url"),
    [
        (
            _item("https://example.com/one", title="后到标题", summary="后到简介"),
            "https://example.com/one",
        ),
        (
            _item("https://example.com/two"),
            "https://example.com/one",
        ),
    ],
)
async def test_same_batch_url_or_fingerprint_collision_uses_first_valid_item(
    database: Database,
    second: CollectedItem,
    expected_url: str,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    first = _item("https://example.com/one")
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[first, second]]

    result = await _pipeline(database, backend).update()

    assert (result.discovered_count, result.new_count, result.updated_count) == (2, 1, 0)
    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        revisions = uow.revisions.list()
    assert len(items) == 1
    assert items[0].canonical_url == expected_url
    assert items[0].title == first.title
    assert revisions == []


@pytest.mark.asyncio
async def test_content_change_creates_revision_with_only_changed_fields(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    published = datetime(2026, 7, 1, tzinfo=UTC)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/1", published_at=published, extra={"attachment": "a.pdf"})],
        [
            _item(
                "https://example.com/1",
                summary="更新后的申报材料说明",
                published_at=datetime(2026, 7, 2),
                extra={"attachment": "b.pdf"},
            )
        ],
    ]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    result = await pipeline.update()

    assert result.updated_count == 1
    with RepositoryUnitOfWork(database) as uow:
        revisions = uow.revisions.list()
    assert len(revisions) == 1
    assert set(revisions[0].old_data) == {"summary", "published_at", "extra"}
    assert set(revisions[0].new_data) == {"summary", "published_at", "extra"}
    assert revisions[0].old_data["extra"] == {"attachment": "a.pdf"}
    assert revisions[0].new_data["extra"] == {"attachment": "b.pdf"}


@pytest.mark.asyncio
async def test_json_key_order_and_equivalent_timezone_do_not_create_revision(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    first_time = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/1", published_at=first_time, extra={"a": 1, "b": 2})],
        [
            _item(
                "https://example.com/1",
                published_at=datetime(
                    2026,
                    7,
                    19,
                    0,
                    0,
                    tzinfo=timezone(timedelta(hours=8)),
                ),
                extra={"b": 2, "a": 1},
            )
        ],
    ]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    result = await pipeline.update()

    assert (result.updated_count, result.skipped_count) == (0, 1)
    assert result.started_at.tzinfo is UTC
    assert result.finished_at.tzinfo is UTC
    with RepositoryUnitOfWork(database) as uow:
        assert uow.revisions.list() == []


@pytest.mark.asyncio
async def test_business_extra_list_order_is_a_revision_worthy_change(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/1", extra={"attachments": ["a", "b"]})],
        [_item("https://example.com/1", extra={"attachments": ["b", "a"]})],
    ]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    result = await pipeline.update()

    assert result.updated_count == 1
    with RepositoryUnitOfWork(database) as uow:
        revision = uow.revisions.list()[0]
    assert revision.old_data == {"extra": {"attachments": ["a", "b"]}}
    assert revision.new_data == {"extra": {"attachments": ["b", "a"]}}


@pytest.mark.asyncio
async def test_last_seen_only_change_does_not_create_revision(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    collected = _item("https://example.com/1")
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[collected], [collected]]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    with RepositoryUnitOfWork(database) as uow:
        before = uow.items.list()[0].last_seen_at
    result = await pipeline.update()
    with RepositoryUnitOfWork(database) as uow:
        after = uow.items.list()[0].last_seen_at
        revisions = uow.revisions.list()

    assert result.skipped_count == 1
    assert after >= before
    assert revisions == []


@pytest.mark.asyncio
async def test_manual_category_is_preserved_while_automatic_fields_refresh(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/1")],
        [_item("https://example.com/1", title="Qwen Agent 产品版本正式发布")],
    ]
    pipeline = _pipeline(database, backend)
    await pipeline.update()
    with RepositoryUnitOfWork(database) as uow:
        item = uow.items.list()[0]
        item.manual_category = Category.AWARD_CASE
        item.is_favorite = True

    result = await pipeline.update()

    with RepositoryUnitOfWork(database) as uow:
        item = uow.items.list()[0]
        revisions = uow.revisions.list()
    assert result.updated_count == 1
    assert item.manual_category is Category.AWARD_CASE
    assert item.category is Category.AGENT_PRODUCT
    assert (item.manual_category or item.category) is Category.AWARD_CASE
    assert item.is_favorite is True
    assert len(revisions) == 1


@pytest.mark.asyncio
async def test_unclassified_count_is_aggregated(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/1", title="Weekly notes", summary=None)]
    ]

    result = await _pipeline(database, backend).update()

    assert result.unclassified_count == 1
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.list()[0].category is Category.UNCLASSIFIED


@pytest.mark.asyncio
async def test_source_failure_is_isolated_and_produces_partial_success(database: Database) -> None:
    failing = _source("Failing", "https://example.com/fail")
    working = _source("Working", "https://example.com/work")
    _add_sources(database, failing, working)
    backend = ScenarioFetcher()
    backend.responses[failing.start_url] = [
        RuntimeError(
            "<html>huge body</html> token=super-secret "
            "https://url-user:url-pass@x.test/a?token=secret"
        )
    ]
    backend.responses[working.start_url] = [[_item("https://example.com/ok")]]

    result = await _pipeline(database, backend).update()

    assert result.status is CrawlStatus.PARTIAL_SUCCESS
    assert (result.source_success, result.source_failed, result.new_count) == (1, 1, 1)
    assert [entry.status for entry in result.source_results] == [
        SourceUpdateStatus.FAILED,
        SourceUpdateStatus.SUCCESS,
    ]
    assert result.failure_reason_counts == {"parse_or_collection.failed": 1}
    failed_error = result.source_results[0].error or ""
    assert "super-secret" not in failed_error
    assert "<html>" not in failed_error
    assert "?token=" not in failed_error
    assert "url-user" not in failed_error
    assert "url-pass" not in failed_error
    with RepositoryUnitOfWork(database) as uow:
        failed_source = uow.sources.get(failing.id)
        working_source = uow.sources.get(working.id)
    assert failed_source is not None and failed_source.last_checked_at is not None
    assert failed_source.last_success_at is None
    assert failed_source.last_error == failed_error
    assert working_source is not None and working_source.last_success_at is not None


@pytest.mark.asyncio
async def test_all_sources_failed_marks_run_failed_and_finished(database: Database) -> None:
    first = _source("First", "https://example.com/1")
    second = _source("Second", "https://example.com/2")
    _add_sources(database, first, second)
    backend = ScenarioFetcher()
    backend.responses[first.start_url] = [RuntimeError("first failed")]
    backend.responses[second.start_url] = [RuntimeError("second failed")]

    result = await _pipeline(database, backend).update()

    assert result.status is CrawlStatus.FAILED
    assert (result.source_success, result.source_failed) == (0, 2)
    assert result.finished_at is not None
    with RepositoryUnitOfWork(database) as uow:
        runs = uow.crawl_runs.list()
    assert len(runs) == 1
    assert runs[0].status is CrawlStatus.FAILED
    assert runs[0].finished_at is not None


@pytest.mark.asyncio
async def test_disabled_sources_are_skipped_by_default_and_can_be_explicitly_run(
    database: Database,
) -> None:
    enabled = _source("Enabled", "https://example.com/enabled")
    disabled = _source("Disabled", "https://example.com/disabled", enabled=False)
    _add_sources(database, enabled, disabled)
    backend = ScenarioFetcher()
    backend.responses[enabled.start_url] = [[]]
    backend.responses[disabled.start_url] = [[]]
    pipeline = _pipeline(database, backend)

    default_result = await pipeline.update()
    assert default_result.source_total == 1
    assert [context.source_url for context in backend.contexts] == [enabled.start_url]
    with pytest.raises(SourceDisabledError, match="allow_disabled=True"):
        await pipeline.update(source_id=disabled.id)
    explicit_result = await pipeline.update(source_id=disabled.id, allow_disabled=True)
    assert explicit_result.source_total == 1
    assert backend.contexts[-1].source_url == disabled.start_url


@pytest.mark.asyncio
async def test_formal_bulk_update_skips_test_fallback_and_disabled_sources(
    database: Database,
) -> None:
    formal = _source("Formal", "https://example.com/formal")
    formal.source_kind = SourceKind.FORMAL
    test = _source("Test", "https://example.com/test")
    fallback = _source("Fallback", "https://example.com/fallback")
    fallback.source_kind = SourceKind.FALLBACK
    disabled = _source("Disabled formal", "https://example.com/disabled", enabled=False)
    disabled.source_kind = SourceKind.FORMAL
    _add_sources(database, formal, test, fallback, disabled)
    backend = ScenarioFetcher()
    backend.responses[formal.start_url] = [[]]
    backend.responses[test.start_url] = [[]]

    bulk = await _pipeline(database, backend).update(formal_only=True)
    explicit_test = await _pipeline(database, backend).update(source_id=test.id)

    assert bulk.source_total == 1
    assert explicit_test.source_total == 1
    assert [context.source_url for context in backend.contexts] == [
        formal.start_url,
        test.start_url,
    ]


@pytest.mark.asyncio
async def test_failures_are_separate_from_admission_rejections(database: Database) -> None:
    fetch_failure = _source("Fetch failure", "https://example.com/fetch")
    rejected = _source("Rejected", "https://example.com/rejected")
    rejected.minimum_quality_score = 100
    _add_sources(database, fetch_failure, rejected)
    backend = ScenarioFetcher()
    backend.responses[fetch_failure.start_url] = [
        NetworkFetchError(fetch_failure.start_url, "network unavailable")
    ]
    backend.responses[rejected.start_url] = [[_item("https://example.com/rejected/item")]]

    result = await _pipeline(database, backend).update()

    assert result.failed_count == 1
    assert result.failure_reason_counts == {"fetch.failed": 1}
    assert result.rejected_count == 1
    assert result.rejection_reason_counts == {"quality.below_minimum": 1}
    assert result.source_results[0].rejected == 0
    assert result.source_results[1].failed == 0


@pytest.mark.asyncio
async def test_invalid_source_configuration_fails_before_collection(database: Database) -> None:
    source = _source("Invalid", "https://example.com/invalid")
    source.content_scope = cast(list[str], None)
    _add_sources(database, source)
    backend = ScenarioFetcher()

    result = await _pipeline(database, backend).update()

    assert result.source_failed == 1
    assert result.discovered_count == 0
    assert result.rejected_count == 0
    assert result.failure_reason_counts == {"source.configuration_invalid": 1}
    assert backend.contexts == []


@pytest.mark.asyncio
async def test_saved_source_preview_reports_primary_rejection_without_writes(
    database: Database,
) -> None:
    source = _source("Preview", "https://example.com/preview")
    source.minimum_quality_score = 50
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [
            _item(
                "https://example.com/preview/recruitment",
                title="人工智能企业校园招聘公告",
            ),
            _item("https://example.com/preview/release", title="新一代大模型正式发布"),
        ]
    ]

    result = await _pipeline(database, backend).preview(source.id)

    assert (result.fetched, result.normalized, result.accepted, result.rejected) == (2, 2, 1, 1)
    assert result.primary_rejection_reason == "content.recruitment"
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.list() == []
        assert uow.crawl_runs.list() == []


@pytest.mark.asyncio
async def test_scheduled_update_selects_only_enabled_sources_and_marks_run(
    database: Database,
) -> None:
    enabled = _source("Enabled", "https://example.com/scheduled-enabled")
    disabled = _source("Disabled", "https://example.com/scheduled-disabled", enabled=False)
    _add_sources(database, enabled, disabled)
    backend = ScenarioFetcher()
    backend.responses[enabled.start_url] = [[]]

    result = await _pipeline(database, backend).update(trigger=RunTrigger.SCHEDULED)

    assert result.trigger is RunTrigger.SCHEDULED
    assert [context.source_url for context in backend.contexts] == [enabled.start_url]
    with RepositoryUnitOfWork(database) as uow:
        assert uow.crawl_runs.get(result.crawl_run_id).trigger is RunTrigger.SCHEDULED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cancelled_scheduled_update_finishes_crawl_run_and_releases_resources(
    database: Database,
) -> None:
    source = _source("Blocking", "https://example.com/blocking")
    source.collector_name = "blocking-scenario"
    _add_sources(database, source)
    backend = BlockingScenarioFetcher()
    registry = CollectorRegistry()
    registry.register("blocking-scenario", BlockingScenarioCollector)

    def uow_factory() -> RepositoryUnitOfWork:
        return RepositoryUnitOfWork(database)

    pipeline = UpdatePipeline(
        uow_factory=uow_factory,
        crawl_service=CrawlService(registry, backend),
        classification_service=ClassificationService(RuleBasedClassifier.from_yaml(), uow_factory),
    )
    task = asyncio.create_task(pipeline.update(trigger=RunTrigger.SCHEDULED))
    await asyncio.wait_for(backend.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with RepositoryUnitOfWork(database) as uow:
        runs = uow.crawl_runs.list_recent()
    assert len(runs) == 1
    assert runs[0].status is CrawlStatus.FAILED
    assert runs[0].finished_at is not None
    assert runs[0].trigger is RunTrigger.SCHEDULED
    assert "cancelled" in (runs[0].error_summary or "")


@pytest.mark.asyncio
async def test_source_id_selection_and_missing_source_error(database: Database) -> None:
    first = _source("First", "https://example.com/1")
    second = _source("Second", "https://example.com/2")
    _add_sources(database, first, second)
    backend = ScenarioFetcher()
    backend.responses[second.start_url] = [[]]
    pipeline = _pipeline(database, backend)

    result = await pipeline.update(source_id=second.id)

    assert result.source_total == 1
    assert result.source_results[0].source_id == second.id
    with pytest.raises(SourceNotFoundError, match="999999"):
        await pipeline.update(source_id=999_999)


@pytest.mark.asyncio
async def test_invalid_items_are_isolated_but_all_invalid_fails_source(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    invalid_title = _item("https://example.com/empty", title="   ")
    invalid_url = _item("javascript:alert(1)")
    invalid_extra = _item("https://example.com/bad-extra", extra={"value": object()})
    valid = _item("https://example.com/valid")
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [invalid_title, invalid_url, invalid_extra, valid],
        [invalid_title, invalid_url],
    ]
    pipeline = _pipeline(database, backend)

    first = await pipeline.update()
    second = await pipeline.update()

    assert first.status is CrawlStatus.SUCCESS
    assert (first.discovered_count, first.new_count, first.skipped_count) == (4, 1, 3)
    assert second.status is CrawlStatus.FAILED
    assert (second.discovered_count, second.skipped_count) == (2, 2)
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.items.list()) == 1


@pytest.mark.asyncio
async def test_cross_source_url_keeps_first_item_and_records_discovery(database: Database) -> None:
    first = _source("First", "https://example.com/feed-1")
    second = _source("Second", "https://example.com/feed-2")
    _add_sources(database, first, second)
    shared_url = "https://shared.example.com/article"
    backend = ScenarioFetcher()
    backend.responses[first.start_url] = [[_item(shared_url)]]
    backend.responses[second.start_url] = [
        [_item(shared_url, title="Qwen Agent 产品版本正式发布", extra={"other": True})]
    ]
    pipeline = _pipeline(database, backend)
    await pipeline.update(source_id=first.id)
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.list()[0]
        discovered_at = stored.discovered_at
        stored.manual_category = Category.ENTERPRISE_CASE
        stored.is_favorite = True

    result = await pipeline.update(source_id=second.id)

    assert (result.new_count, result.updated_count, result.skipped_count) == (0, 0, 1)
    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        revisions = uow.revisions.list()
    assert len(items) == 1
    stored = items[0]
    assert stored.source_id == first.id
    assert stored.discovered_at == discovered_at
    assert stored.title == "关于开展优秀人工智能案例征集的通知"
    assert stored.category is Category.SOLICITATION
    assert stored.manual_category is Category.ENTERPRISE_CASE
    assert stored.is_favorite is True
    discoveries = stored.extra["_source_discoveries"]
    assert isinstance(discoveries, list)
    assert discoveries[0]["source_id"] == second.id
    assert revisions == []


@pytest.mark.asyncio
async def test_cross_source_reserved_metadata_is_stable_and_not_business_content(
    database: Database,
) -> None:
    first = _source("First", "https://example.com/feed-1")
    second = _source("Second", "https://example.com/feed-2")
    third = _source("Third", "https://example.com/feed-3")
    _add_sources(database, first, second, third)
    shared_url = "https://shared.example.com/article"
    forged = {
        "business": True,
        "_source_discoveries": [
            {
                "source_id": 999,
                "source_name": "forged",
                "first_seen_at": "never",
                "last_seen_at": "never",
            }
        ],
    }
    backend = ScenarioFetcher()
    backend.responses[first.start_url] = [[_item(shared_url, extra=forged)]]
    backend.responses[second.start_url] = [
        [_item(shared_url, extra={"_source_discoveries": "forged"})],
        [_item(shared_url, extra={"_source_discoveries": []})],
    ]
    backend.responses[third.start_url] = [[_item(shared_url)]]
    pipeline = _pipeline(database, backend)

    await pipeline.update(source_id=first.id)
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.list()[0].extra == {"business": True}

    await pipeline.update(source_id=third.id)
    first_discovery = await pipeline.update(source_id=second.id)
    with RepositoryUnitOfWork(database) as uow:
        stored_after_first = uow.items.list()[0]
        discoveries_after_first = stored_after_first.extra["_source_discoveries"]
        first_seen = discoveries_after_first[0]["first_seen_at"]
    repeated = await pipeline.update(source_id=second.id)

    assert (first_discovery.updated_count, first_discovery.skipped_count) == (0, 1)
    assert (repeated.updated_count, repeated.skipped_count) == (0, 1)
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.list()[0]
        revisions = uow.revisions.list()
    discoveries = stored.extra["_source_discoveries"]
    assert [entry["source_id"] for entry in discoveries] == [second.id, third.id]
    assert discoveries[0]["first_seen_at"] == first_seen
    assert discoveries[0]["last_seen_at"] >= first_seen
    assert stored.extra["business"] is True
    assert revisions == []


@pytest.mark.asyncio
async def test_cross_source_discovery_recovers_legacy_non_object_extra(database: Database) -> None:
    first = _source("First", "https://example.com/feed-1")
    second = _source("Second", "https://example.com/feed-2")
    _add_sources(database, first, second)
    shared_url = "https://shared.example.com/article"
    backend = ScenarioFetcher()
    backend.responses[first.start_url] = [[_item(shared_url)]]
    backend.responses[second.start_url] = [[_item(shared_url)]]
    pipeline = _pipeline(database, backend)
    await pipeline.update(source_id=first.id)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE intelligence_items SET extra = '[]'")

    result = await pipeline.update(source_id=second.id)

    assert result.status is CrawlStatus.SUCCESS
    assert (result.updated_count, result.skipped_count) == (0, 1)
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.list()[0]
    assert stored.extra["_source_discoveries"][0]["source_id"] == second.id


@pytest.mark.asyncio
async def test_same_source_fingerprint_deduplicates_a_changed_url(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/old-url")],
        [_item("https://example.com/new-url")],
    ]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    result = await pipeline.update()

    assert (result.new_count, result.updated_count, result.skipped_count) == (0, 0, 1)
    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
    assert len(items) == 1
    assert items[0].canonical_url == "https://example.com/old-url"


@pytest.mark.asyncio
async def test_changed_url_and_changed_short_title_are_not_fuzzily_merged(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item("https://example.com/one", title="AI 周报 1")],
        [_item("https://example.com/two", title="AI 周报 2")],
    ]
    pipeline = _pipeline(database, backend)

    await pipeline.update()
    result = await pipeline.update()

    assert (result.new_count, result.updated_count, result.skipped_count) == (1, 0, 0)
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.items.list()) == 2


@pytest.mark.asyncio
async def test_history_mode_options_reach_collector_config(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[]]

    await _pipeline(database, backend).update(
        mode=UpdateMode.HISTORY,
        max_pages=999,
        max_items=999_999,
        published_from=datetime(2025, 1, 1),
        published_to=datetime(2026, 1, 1, tzinfo=UTC),
    )

    config = backend.contexts[0].config
    assert config["update_mode"] == "history"
    assert config["max_items"] == 999_999
    assert config["published_from"] == "2025-01-01T00:00:00+00:00"
    discovery = config["discovery"]
    assert isinstance(discovery, dict)
    assert discovery["max_pages"] == 999
    assert discovery["max_items"] == 999_999


class FailFirstFinishService(CrawlRunService):
    def __init__(self, database: Database) -> None:
        super().__init__(lambda: RepositoryUnitOfWork(database))
        self.failed_once = False

    def finish(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if not self.failed_once:
            self.failed_once = True
            raise RuntimeError("unexpected finalization failure")
        return super().finish(*args, **kwargs)  # pyright: ignore[reportArgumentType]


class AlwaysFailFinishService(CrawlRunService):
    def __init__(self, database: Database) -> None:
        super().__init__(lambda: RepositoryUnitOfWork(database))
        self.calls = 0

    def finish(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        del args, kwargs
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("original finalization failure")
        raise RuntimeError("failed-state persistence failure")


@pytest.mark.asyncio
async def test_uncaught_exception_still_moves_run_out_of_running(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[]]
    service = FailFirstFinishService(database)

    with pytest.raises(RuntimeError, match="unexpected finalization"):
        await _pipeline(database, backend, crawl_run_service=service).update()

    with RepositoryUnitOfWork(database) as uow:
        run = uow.crawl_runs.list()[0]
    assert run.status is CrawlStatus.FAILED
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_failed_state_persistence_does_not_recurse_or_mask_original_error(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[]]
    service = AlwaysFailFinishService(database)

    with pytest.raises(RuntimeError, match="original finalization failure"):
        await _pipeline(database, backend, crawl_run_service=service).update()

    assert service.calls == 2


def test_unique_constraint_conflict_recovers_inside_savepoint(database: Database) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    with RepositoryUnitOfWork(database) as uow:
        existing = uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="Existing",
                original_url="https://example.com/item",
                canonical_url="https://example.com/item",
                fingerprint="a" * 64,
            )
        )

    with RepositoryUnitOfWork(database) as uow:
        recovered, inserted = uow.items.add_or_get_existing(
            IntelligenceItem(
                source_id=source.id,
                title="Racing duplicate",
                original_url="https://example.com/item",
                canonical_url="https://example.com/item",
                fingerprint="b" * 64,
            )
        )
        source_in_same_transaction = uow.sources.get(source.id)
        assert source_in_same_transaction is not None
        source_in_same_transaction.last_error = "transaction remains usable"
        following = uow.items.add_or_get_existing(
            IntelligenceItem(
                source_id=source.id,
                title="Following item",
                original_url="https://example.com/following",
                canonical_url="https://example.com/following",
                fingerprint="c" * 64,
            )
        )

    assert inserted is False
    assert recovered.id == existing.id
    assert following[1] is True
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.items.list()) == 2
        assert uow.sources.get(source.id).last_error == "transaction remains usable"  # type: ignore[union-attr]


class CommitFailureController:
    def __init__(self, fail_on: set[int]) -> None:
        self.commit_attempts = 0
        self.fail_on = fail_on


class FailingCommitUnitOfWork(TrackingUnitOfWork):
    def __init__(
        self,
        database: Database,
        tracker: ScenarioFetcher,
        controller: CommitFailureController,
    ) -> None:
        super().__init__(database, tracker)
        self._controller = controller

    def __exit__(self, *args: object) -> None:
        if args and args[0] is None:
            self._controller.commit_attempts += 1
            if self._controller.commit_attempts in self._controller.fail_on:
                failure = RuntimeError("injected transaction commit failure")
                super().__exit__(type(failure), failure, None)
                raise failure
        super().__exit__(*args)


@pytest.mark.asyncio
async def test_source_commit_failure_rolls_back_counts_and_later_source_continues(
    database: Database,
) -> None:
    failing = _source("Failing", "https://example.com/failing")
    working = _source("Working", "https://example.com/working")
    _add_sources(database, failing, working)
    backend = ScenarioFetcher()
    backend.responses[failing.start_url] = [[_item("https://example.com/rolled-back")]]
    backend.responses[working.start_url] = [[_item("https://example.com/committed")]]
    controller = CommitFailureController({3})

    def uow_factory() -> RepositoryUnitOfWork:
        return FailingCommitUnitOfWork(database, backend, controller)

    result = await _pipeline(database, backend, uow_factory=uow_factory).update()

    assert result.status is CrawlStatus.PARTIAL_SUCCESS
    assert (result.source_success, result.source_failed) == (1, 1)
    assert (result.new_count, result.updated_count) == (1, 0)
    assert [entry.status for entry in result.source_results] == [
        SourceUpdateStatus.FAILED,
        SourceUpdateStatus.SUCCESS,
    ]
    assert result.failure_reason_counts == {"persistence.failed": 1}
    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        failed_source = uow.sources.get(failing.id)
        run = uow.crawl_runs.list()[0]
    assert [item.canonical_url for item in items] == ["https://example.com/committed"]
    assert failed_source is not None
    assert failed_source.last_checked_at is not None
    assert failed_source.last_success_at is None
    assert run.new_count == 1
    assert run.finished_at is not None


@pytest.mark.asyncio
async def test_source_commit_failure_rolls_back_item_update_and_revision(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    item_url = "https://example.com/existing"
    with RepositoryUnitOfWork(database) as uow:
        uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="Existing",
                original_url=item_url,
                canonical_url=item_url,
                summary="old summary",
                fingerprint=generate_item_fingerprint("Existing"),
            )
        )
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [
        [_item(item_url, title="Existing", summary="new summary")]
    ]
    controller = CommitFailureController({3})

    def uow_factory() -> RepositoryUnitOfWork:
        return FailingCommitUnitOfWork(database, backend, controller)

    result = await _pipeline(database, backend, uow_factory=uow_factory).update()

    assert result.status is CrawlStatus.FAILED
    assert (result.new_count, result.updated_count) == (0, 0)
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.list()[0]
        revisions = uow.revisions.list()
    assert stored.summary == "old summary"
    assert revisions == []


@pytest.mark.asyncio
async def test_source_failure_state_commit_failure_does_not_stop_later_sources(
    database: Database,
) -> None:
    failing = _source("Failing", "https://example.com/failing")
    working = _source("Working", "https://example.com/working")
    _add_sources(database, failing, working)
    backend = ScenarioFetcher()
    backend.responses[failing.start_url] = [RuntimeError("network failed")]
    backend.responses[working.start_url] = [[_item("https://example.com/committed")]]
    # Selection and CrawlRun start are commits 1 and 2; source failure state is commit 3.
    controller = CommitFailureController({3})

    def uow_factory() -> RepositoryUnitOfWork:
        return FailingCommitUnitOfWork(database, backend, controller)

    result = await _pipeline(database, backend, uow_factory=uow_factory).update()

    assert result.status is CrawlStatus.PARTIAL_SUCCESS
    assert (result.source_success, result.source_failed, result.new_count) == (1, 1, 1)
    with RepositoryUnitOfWork(database) as uow:
        assert [item.canonical_url for item in uow.items.list()] == [
            "https://example.com/committed"
        ]
        assert uow.crawl_runs.list()[0].finished_at is not None


@pytest.mark.asyncio
async def test_crawl_run_finish_commit_failure_uses_independent_failed_transaction(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    backend = ScenarioFetcher()
    backend.responses[source.start_url] = [[_item("https://example.com/committed")]]
    # Selection, CrawlRun start and source persistence are commits 1-3; finalization is 4.
    controller = CommitFailureController({4})

    def uow_factory() -> RepositoryUnitOfWork:
        return FailingCommitUnitOfWork(database, backend, controller)

    with pytest.raises(RuntimeError, match="injected transaction commit failure"):
        await _pipeline(database, backend, uow_factory=uow_factory).update()

    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        run = uow.crawl_runs.list()[0]
    assert [item.canonical_url for item in items] == ["https://example.com/committed"]
    assert run.status is CrawlStatus.FAILED
    assert run.finished_at is not None
    assert (run.source_success, run.source_failed, run.new_count) == (1, 0, 1)


def test_application_services_do_not_expose_sqlalchemy_session(database: Database) -> None:
    backend = ScenarioFetcher()
    pipeline = _pipeline(database, backend)
    persistence = ItemPersistenceService(lambda: RepositoryUnitOfWork(database))

    assert not hasattr(pipeline, "session")
    assert not hasattr(persistence, "session")
    with RepositoryUnitOfWork(database) as uow:
        assert not hasattr(uow, "session")


def test_fingerprint_is_stable_for_equivalent_titles_and_source_scoped() -> None:
    first = generate_item_fingerprint("  Qwen\tAgent Ｖ2.0  ")  # noqa: RUF001
    second = generate_item_fingerprint("qwen agent V2.0")

    assert first == second
    assert len(first) == 64
    assert generate_item_fingerprint("Qwen Agent V2.1") != first


@pytest.mark.asyncio
async def test_reclassify_item_and_all_are_independent_service_interfaces(
    database: Database,
) -> None:
    source = _source("Feed", "https://example.com/feed")
    _add_sources(database, source)
    with RepositoryUnitOfWork(database) as uow:
        item = uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="人工智能行业标准正式发布",
                original_url="https://example.com/item",
                canonical_url="https://example.com/item",
                category=Category.UNCLASSIFIED,
                manual_category=Category.ENTERPRISE_CASE,
                fingerprint="f" * 64,
            )
        )
    service = ClassificationService(
        RuleBasedClassifier.from_yaml(),
        lambda: RepositoryUnitOfWork(database),
    )

    result = await service.reclassify_item(item.id)
    count = await service.reclassify_all(source_id=source.id)

    assert result.category is Category.POLICY_INDUSTRY
    assert count == 1
    with RepositoryUnitOfWork(database) as uow:
        stored = uow.items.get(item.id)
    assert stored is not None
    assert stored.category is Category.POLICY_INDUSTRY
    assert stored.manual_category is Category.ENTERPRISE_CASE
    assert stored.classification_reason
