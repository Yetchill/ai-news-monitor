"""Deterministic release-stability coverage for the local update runtime.

This is deliberately an offline test: every crawler response is a checked-in
fixture and classification stays on the local rule provider.  It exercises the
same collector registry and update pipeline used by manual and scheduled runs.
"""

import asyncio
import json
import os
import resource
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import AsyncGenerator, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest

from app import __version__
from app.classifiers.rule_based import RuleBasedClassifier
from app.collectors.registry import default_collector_registry
from app.domain.collection import FetchResult
from app.domain.enums import CrawlStatus, RunTrigger, SourceOrigin, SourceType, Weekday
from app.domain.models import Source
from app.domain.update import UpdateResult
from app.fetchers.errors import (
    FetchError,
    FetchTimeoutError,
    ForbiddenFetchError,
    NetworkFetchError,
    RateLimitFetchError,
    ServerFetchError,
)
from app.services.background_tasks import BackgroundTaskManager
from app.services.classification_service import ClassificationService
from app.services.crawl_service import CrawlService
from app.services.schedule_settings_service import ScheduleSettingsService
from app.services.scheduler_service import SchedulerService
from app.services.update_execution_service import (
    UpdateExecutionService,
    UpdateLock,
)
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RELEASE_VALIDATION = Path(__file__).resolve().parents[2] / "release-validation.json"


class OfflineFixtureFetcher:
    """Transport double that rejects any request not explicitly fixed in the test."""

    def __init__(
        self,
        responses: Mapping[str, bytes],
        *,
        fail_once: set[str] | None = None,
        cycle_faults: Mapping[int, Mapping[str, "FaultFactory"]] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._fail_once = fail_once or set()
        self._cycle_faults = cycle_faults or {}
        self.cycle = 0
        self.requests: list[str] = []

    def set_cycle(self, cycle: int) -> None:
        self.cycle = cycle

    async def fetch(self, url: str, *, headers: Mapping[str, str] | None = None) -> FetchResult:
        del headers
        self.requests.append(url)
        if url in self._fail_once:
            self._fail_once.remove(url)
            raise NetworkFetchError(url, "deterministic fixture outage")
        fault_key = (
            "https://api.github.com/repos/QwenLM/Qwen-Agent/releases"
            if url.startswith("https://api.github.com/repos/QwenLM/Qwen-Agent/releases?")
            else url
        )
        fault = self._cycle_faults.get(self.cycle, {}).get(fault_key)
        if fault is not None:
            outcome = fault(url)
            if isinstance(outcome, FetchError):
                raise outcome
            return FetchResult(
                requested_url=url,
                url=url,
                status_code=200,
                headers={},
                content=outcome,
            )
        content = self._responses.get(url)
        if content is None and url.startswith(
            "https://api.github.com/repos/QwenLM/Qwen-Agent/releases?"
        ):
            content = self._responses["https://api.github.com/repos/QwenLM/Qwen-Agent/releases"]
        if content is None:
            raise AssertionError(f"unexpected network request: {url}")
        return FetchResult(
            requested_url=url,
            url=url,
            status_code=200,
            headers={},
            content=content,
        )


FaultFactory = Callable[[str], bytes | FetchError]


class _CheckedOutPool(Protocol):
    def checkedout(self) -> int: ...


class AcceleratedClock:
    """Advance a scheduler one planned target at a time with no wall-clock sleep."""

    def __init__(self, current: datetime) -> None:
        self.current = current
        self.targets: asyncio.Queue[datetime] = asyncio.Queue()
        self._advance = asyncio.Event()

    def now(self) -> datetime:
        return self.current

    async def wait_until(self, target: datetime, wake_event: asyncio.Event) -> bool:
        del wake_event
        self.targets.put_nowait(target)
        await self._advance.wait()
        self._advance.clear()
        self.current = target
        return False

    async def advance(self) -> datetime:
        target = await asyncio.wait_for(self.targets.get(), timeout=1)
        self._advance.set()
        await asyncio.sleep(0)
        return target


class FixturePipelineScheduledRunner:
    """Run a real pipeline once per scheduler target, entirely from local fixtures."""

    def __init__(
        self,
        execution: UpdateExecutionService,
        fetcher: OfflineFixtureFetcher,
        database: Database,
    ) -> None:
        self._execution = execution
        self._fetcher = fetcher
        self._database = database
        self.calls = 0
        self.results: list[UpdateResult] = []
        self.checked_out_samples: list[int] = []
        self.task_samples: list[int] = []
        self.thread_samples: list[int] = []
        self.rss_samples: list[int] = []
        self.tracemalloc_current_samples: list[int] = []

    async def try_scheduled_update(
        self, *, before_update: Callable[[], None] | None = None
    ) -> UpdateResult | None:
        self.calls += 1
        self._fetcher.set_cycle(self.calls)
        result = await self._execution.try_scheduled_update(before_update=before_update)
        assert result is not None
        self.results.append(result)
        self.checked_out_samples.append(_checked_out_connections(self._database))
        self.task_samples.append(len(asyncio.all_tasks()))
        self.thread_samples.append(threading.active_count())
        self.rss_samples.append(_current_rss_bytes())
        self.tracemalloc_current_samples.append(tracemalloc.get_traced_memory()[0])
        return result


def _source(
    name: str,
    source_type: SourceType,
    start_url: str,
    collector_name: str,
    collector_config: dict[str, object],
) -> Source:
    return Source(
        name=name,
        source_type=source_type,
        start_url=start_url,
        collector_name=collector_name,
        collector_config=collector_config,
        origin=SourceOrigin.PRESET,
        minimum_quality_score=0,
        allow_external_links=True,
    )


def _fixture_sources() -> list[Source]:
    return [
        _source(
            "Fixture RSS",
            SourceType.RSS,
            "https://fixtures.example/rss.xml",
            "rss",
            {},
        ),
        _source(
            "Fixture HTML",
            SourceType.HTML_LIST,
            "https://fixtures.example/news",
            "html_list",
            {
                "allowed_domains": ["fixtures.example"],
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
        ),
        _source(
            "Fixture JSON",
            SourceType.JSON_API,
            "https://fixtures.example/api/qwen",
            "public_json",
            {
                "query_params": "kind=fixture",
                "items_field": "data.articles",
                "link_template": "https://fixtures.example/blog/{path}",
                "date_field": "date",
                "date_nested_in": "extra",
                "date_format": "iso",
                "response_limit_bytes": 10_000,
            },
        ),
        _source(
            "Fixture GitHub releases",
            SourceType.GITHUB_RELEASE,
            "https://github.com/QwenLM/Qwen-Agent/releases",
            "github_release",
            {"allow_technical_updates": True},
        ),
        _source(
            "Fixture single-page changelog",
            SourceType.CUSTOM,
            "https://fixtures.example/changelog",
            "single_page_changelog",
            {
                "content_selector": ".theme-doc-markdown",
                "date_heading_selector": "h2",
                "entry_selector": "h3",
                "append_summary_when_title_lacks_action": True,
            },
        ),
        _source(
            "Fixture recovery RSS",
            SourceType.RSS,
            "https://fixtures.example/recovery.xml",
            "rss",
            {},
        ),
    ]


def _responses() -> dict[str, bytes]:
    return {
        "https://fixtures.example/rss.xml": (FIXTURES / "sample_rss.xml").read_bytes(),
        "https://fixtures.example/news": (FIXTURES / "sample_list.html").read_bytes(),
        "https://fixtures.example/news?page=2": (FIXTURES / "sample_list_page_2.html").read_bytes(),
        "https://fixtures.example/api/qwen?kind=fixture": (
            FIXTURES / "qwen_blog.json"
        ).read_bytes(),
        "https://api.github.com/repos/QwenLM/Qwen-Agent/releases?per_page=30": (
            FIXTURES / "github_releases.json"
        ).read_bytes(),
        "https://api.github.com/repos/QwenLM/Qwen-Agent/releases": (
            FIXTURES / "github_releases.json"
        ).read_bytes(),
        "https://fixtures.example/changelog": (FIXTURES / "deepseek_changelog.html").read_bytes(),
        "https://fixtures.example/recovery.xml": (FIXTURES / "sample_atom.xml").read_bytes(),
    }


def _pipeline(database: Database, fetcher: OfflineFixtureFetcher) -> UpdatePipeline:
    def uow_factory() -> RepositoryUnitOfWork:
        return RepositoryUnitOfWork(database)

    return UpdatePipeline(
        uow_factory=uow_factory,
        crawl_service=CrawlService(default_collector_registry(), fetcher),
        classification_service=ClassificationService(RuleBasedClassifier.from_yaml(), uow_factory),
        initial_fetch_days_provider=lambda: 3650,
        now=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )


def _add_sources(database: Database, sources: list[Source]) -> None:
    with RepositoryUnitOfWork(database) as uow:
        for source in sources:
            uow.sources.add(source)


def _checked_out_connections(database: Database) -> int:
    pool = cast(_CheckedOutPool, database.engine.pool)
    return pool.checkedout()


def _current_rss_bytes() -> int:
    """Read current resident memory; ru_maxrss is intentionally not used for start/end."""

    if sys.platform.startswith("linux"):
        try:
            resident_pages = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (IndexError, OSError, ValueError):
            pass
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(completed.stdout.strip()) * 1024

    # The CI targets are Linux and macOS. This fallback retains a usable number
    # for other developer platforms but is labelled by the report mode below.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def _report_path(tmp_path: Path) -> Path:
    configured = os.environ.get("AIM_RELEASE_VALIDATION_REPORT")
    if not configured:
        return tmp_path / "release-validation.json"
    path = Path(configured).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _assert_no_sustained_growth(
    samples: list[int], *, metric: str, absolute_allowance: int, relative_allowance: float
) -> None:
    """Allow allocator noise while rejecting sustained late-cycle growth.

    The first half includes source creation, SQLite statement caches, and fault
    recovery. Comparing two ten-cycle windows in the settled second half avoids
    treating those one-time allocations as a leak. The metric-specific absolute
    and relative tolerances account for normal allocator and OS RSS jitter.
    """

    assert len(samples) >= 100
    first_settled = statistics.fmean(samples[50:60])
    last_settled = statistics.fmean(samples[90:100])
    allowed_growth = max(absolute_allowance, first_settled * relative_allowance)
    assert last_settled - first_settled <= allowed_growth, (
        f"{metric} grew persistently after warm-up: "
        f"{first_settled:.0f} -> {last_settled:.0f} bytes (allowance {allowed_growth:.0f})"
    )


def _object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _json_path_exists(payload: object, path: str) -> bool:
    current = payload
    for part in path.split("."):
        mapping = _object_mapping(current)
        if mapping is None or part not in mapping:
            return False
        current = mapping[part]
    return current is not None


def _required_fields(manifest: dict[str, object]) -> list[str]:
    raw_fields = manifest["required_fields"]
    if not isinstance(raw_fields, list):
        raise AssertionError("release validation manifest required_fields must be a list")
    fields: list[str] = []
    for field in cast(list[object], raw_fields):
        if not isinstance(field, str):
            raise AssertionError("release validation manifest required_fields must contain strings")
        fields.append(field)
    return fields


def _git_revision() -> str:
    completed = subprocess.run(
        ["git", "-C", str(RELEASE_VALIDATION.parent), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _faults() -> dict[int, dict[str, FaultFactory]]:
    json_url = "https://fixtures.example/api/qwen?kind=fixture"
    github_url = "https://api.github.com/repos/QwenLM/Qwen-Agent/releases"
    return {
        1: {
            "https://fixtures.example/rss.xml": lambda url: FetchTimeoutError(
                url, "deterministic timeout"
            )
        },
        2: {
            "https://fixtures.example/news": lambda url: ForbiddenFetchError(
                url, "HTTP 403 deterministic denial"
            )
        },
        3: {json_url: lambda url: RateLimitFetchError(url, "HTTP 429 deterministic limit")},
        4: {github_url: lambda url: ServerFetchError(url, "HTTP 500 deterministic failure")},
        5: {json_url: lambda _url: b"{malformed fixture json"},
        6: {json_url: lambda _url: b"[" + (b" " * 10_001) + b"]"},
    }


@pytest.mark.asyncio
async def test_release_stability_fixed_collectors_incremental_recovery_and_report(
    database: Database, tmp_path: Path
) -> None:
    """Run the real pipeline three times against every release-critical collector type."""

    sources = _fixture_sources()
    _add_sources(database, sources)
    fetcher = OfflineFixtureFetcher(
        _responses(), fail_once={"https://fixtures.example/recovery.xml"}
    )
    pipeline = _pipeline(database, fetcher)

    first = await pipeline.update(trigger=RunTrigger.MANUAL_CLI)
    second = await pipeline.update(trigger=RunTrigger.MANUAL_CLI)
    third = await pipeline.update(trigger=RunTrigger.MANUAL_CLI)

    assert first.status is CrawlStatus.PARTIAL_SUCCESS
    assert first.source_success == 5
    assert first.source_failed == 1
    assert second.status is CrawlStatus.SUCCESS
    assert second.new_count == 1  # the isolated RSS source recovered
    assert third.status is CrawlStatus.SUCCESS
    assert third.new_count == 0
    assert third.updated_count == 0
    assert third.skipped_count + third.rejected_count == third.discovered_count

    with RepositoryUnitOfWork(database) as uow:
        items = uow.items.list()
        runs = uow.crawl_runs.list_recent(limit=3)
        recovering = uow.sources.get(sources[-1].id)
    assert len(items) == first.new_count + second.new_count
    assert len(runs) == 3
    assert all(run.finished_at is not None for run in runs)
    assert recovering is not None
    assert recovering.last_error is None
    assert recovering.last_success_at is not None
    assert _checked_out_connections(database) == 0


def test_release_validation_manifest_has_required_coverage_fields() -> None:
    manifest = json.loads(RELEASE_VALIDATION.read_text(encoding="utf-8"))
    manifest_data = _object_mapping(manifest)
    assert manifest_data is not None

    assert manifest["mode"] == "deterministic-offline"
    assert manifest["kind"] == "release-validation-schema-baseline"
    assert set(manifest["fixtures"]) == {
        "rss",
        "html",
        "json",
        "github_release",
        "single_page_changelog",
    }
    assert manifest["constraints"] == {
        "cycles": 100,
        "external_ai_tokens": 0,
        "database_checked_out_end": 0,
    }
    assert "runtime.database_checked_out" in _required_fields(manifest_data)


@pytest.mark.asyncio
async def test_release_stability_manual_scheduled_thread_background_and_scheduler_soak(
    database: Database, tmp_path: Path
) -> None:
    """Soak a scheduler with 100 real fixture-pipeline cycles and sampled resources."""

    lock = UpdateLock()
    lease = lock.acquire()
    assert lease is not None
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(lock.acquire).result(timeout=1) is None
    lease.release()

    class BlockingPipeline:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def update(self, **_kwargs: object) -> UpdateResult:
            self.started.set()
            await self.release.wait()
            raise AssertionError("this test only needs lock ownership")

    blocking = BlockingPipeline()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield cast(UpdatePipeline, blocking)

    shared_lock = UpdateLock()
    manual = UpdateExecutionService(database, context, shared_lock)
    scheduled = UpdateExecutionService(database, context, shared_lock)
    manual_task = asyncio.create_task(manual.update(trigger=RunTrigger.MANUAL_WEB))
    await asyncio.wait_for(blocking.started.wait(), timeout=1)
    assert await scheduled.try_scheduled_update() is None
    blocking.release.set()
    with pytest.raises(AssertionError, match="lock ownership"):
        await manual_task
    assert shared_lock.locked is False

    background = BackgroundTaskManager()
    background_release = asyncio.Event()

    async def complete_without_ai() -> None:
        await background_release.wait()

    background.start(complete_without_ai(), name="release-validation-background")
    await asyncio.sleep(0)
    background_start = background.active_count
    background_peak = background.active_count
    background_release.set()
    await asyncio.sleep(0)
    await background.aclose()
    assert background.active_count == 0

    sources = _fixture_sources()
    _add_sources(database, sources)
    fetcher = OfflineFixtureFetcher(_responses(), cycle_faults=_faults())
    pipeline = _pipeline(database, fetcher)

    @asynccontextmanager
    async def pipeline_context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    execution = UpdateExecutionService(database, pipeline_context, UpdateLock())
    settings = ScheduleSettingsService(lambda: RepositoryUnitOfWork(database))
    settings.save(
        enabled=True,
        hour=9,
        minute=0,
        days=tuple(Weekday),
        timezone="UTC",
    )
    clock = AcceleratedClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    runner = FixturePipelineScheduledRunner(execution, fetcher, database)
    scheduler = SchedulerService(settings, runner, clock=clock)
    rss_start = _current_rss_bytes()
    threads_start = threading.active_count()
    tasks_start = len(asyncio.all_tasks())
    checked_out_start = _checked_out_connections(database)
    started = time.perf_counter()
    tracemalloc.start()
    trace_start, _ = tracemalloc.get_traced_memory()
    await scheduler.start()
    targets = [await clock.advance() for _ in range(100)]
    await asyncio.wait_for(_wait_for_calls(runner, 100), timeout=1)
    await scheduler.stop()
    duration_seconds = time.perf_counter() - started
    trace_current, trace_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    result_failures = [result for result in runner.results if result.source_failed]
    failure_text = " ".join(result.error_summary or "" for result in result_failures)
    failure_reasons: dict[str, int] = {}
    for result in runner.results:
        for reason, count in result.failure_reason_counts.items():
            failure_reasons[reason] = failure_reasons.get(reason, 0) + count
    update_new = sum(result.new_count for result in runner.results)
    update_updated = sum(result.updated_count for result in runner.results)
    update_duplicate = sum(result.duplicate_count for result in runner.results)
    update_skipped = sum(result.skipped_count for result in runner.results)

    assert len(set(targets)) == 100
    assert runner.calls == 100
    assert len(runner.results) == 100
    assert all(result.trigger is RunTrigger.SCHEDULED for result in runner.results)
    assert all(result.status is CrawlStatus.PARTIAL_SUCCESS for result in runner.results[:6])
    assert all(result.status is CrawlStatus.SUCCESS for result in runner.results[6:])
    assert sum(result.source_failed for result in runner.results) == 6
    for failure in ("timeout", "403", "429", "500", "Expecting", "exceeds"):
        assert failure in failure_text
    assert failure_reasons == {"fetch.failed": 4, "source.configuration_invalid": 2}
    assert settings.get().last_scheduled_trigger_at == targets[-1]
    assert checked_out_start == 0
    assert all(sample == 0 for sample in runner.checked_out_samples)
    assert _checked_out_connections(database) == 0
    thread_end = threading.active_count()
    tasks_end = len(asyncio.all_tasks())
    rss_end = _current_rss_bytes()
    assert thread_end <= threads_start + 1
    assert tasks_end <= tasks_start + 1
    assert background.active_count == 0
    assert len(runner.tracemalloc_current_samples) == 100
    _assert_no_sustained_growth(
        runner.tracemalloc_current_samples,
        metric="tracemalloc current allocations",
        absolute_allowance=1_000_000,
        relative_allowance=0.30,
    )
    assert len(runner.rss_samples) == 100
    _assert_no_sustained_growth(
        runner.rss_samples,
        metric="current RSS",
        absolute_allowance=8 * 1024 * 1024,
        relative_allowance=0.10,
    )
    assert rss_end - rss_start <= max(16 * 1024 * 1024, rss_start * 0.20)
    assert duration_seconds > 0
    assert update_new > 0
    assert update_duplicate > 0

    report = {
        "schema_version": 2,
        "mode": "deterministic-offline",
        "git": {
            "revision": _git_revision(),
            "version": __version__,
        },
        "duration_seconds": duration_seconds,
        "ai": {"external_tokens": 0, "provider": "rule_based"},
        "crawler": {
            "source_types": ["rss", "html", "json", "github_release", "single_page_changelog"],
            "sources": len(sources),
            "cycles": runner.calls,
            "fault_cycles": {
                "timeout": 1,
                "http_403": 2,
                "http_429": 3,
                "http_500": 4,
                "malformed": 5,
                "oversized": 6,
            },
            "failure_reasons": failure_reasons,
        },
        "updates": {
            "runs": len(runner.results),
            "new": update_new,
            "updated": update_updated,
            "duplicate": update_duplicate,
            "skipped": update_skipped,
        },
        "source_counts": {
            "attempted": sum(result.source_total for result in runner.results),
            "successful": sum(result.source_success for result in runner.results),
            "failed": sum(result.source_failed for result in runner.results),
        },
        "memory": {
            "rss_bytes": {
                "start": rss_start,
                "peak": max([rss_start, *runner.rss_samples]),
                "end": rss_end,
            },
            "tracemalloc_bytes": {
                "start": trace_start,
                "peak": trace_peak,
                "end": trace_current,
            },
        },
        "runtime": {
            "threads": {
                "start": threads_start,
                "peak": max([threads_start, *runner.thread_samples]),
                "end": thread_end,
            },
            "asyncio_tasks": {
                "start": tasks_start,
                "peak": max([tasks_start, *runner.task_samples]),
                "end": tasks_end,
            },
            "background_tasks": {
                "start": background_start,
                "peak": background_peak,
                "end": background.active_count,
            },
            "database_checked_out": {
                "start": checked_out_start,
                "peak": max([checked_out_start, *runner.checked_out_samples]),
                "end": _checked_out_connections(database),
            },
        },
    }
    report_path = _report_path(tmp_path)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    generated_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert generated_report == report
    manifest_data = _object_mapping(json.loads(RELEASE_VALIDATION.read_text(encoding="utf-8")))
    assert manifest_data is not None
    assert all(
        _json_path_exists(generated_report, field) for field in _required_fields(manifest_data)
    )


async def _wait_for_calls(runner: FixturePipelineScheduledRunner, expected: int) -> None:
    while runner.calls < expected:
        await asyncio.sleep(0)
