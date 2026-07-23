# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Stage-seven scheduling tests using controlled clocks and temporary databases."""

import asyncio
import importlib
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import Barrier
from types import TracebackType
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import cli
from app.domain.enums import CrawlStatus, RunTrigger, Weekday
from app.domain.models import ScheduleSettings
from app.domain.scheduling import SchedulerStatus
from app.domain.update import UpdateResult
from app.services import schedule_settings_service as schedule_module
from app.services.crawl_run_service import CrawlRunService
from app.services.schedule_settings_service import (
    ScheduleSettingsService,
    ScheduleValidationError,
    next_scheduled_run,
    parse_time,
    parse_weekdays,
    system_timezone_name,
)
from app.services.scheduler_service import SchedulerService
from app.services.update_execution_service import (
    UpdateExecutionService,
    UpdateInProgressError,
    UpdateLock,
)
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app
from app.web.dependencies import WebUpdateService

web_app_module = importlib.import_module("app.web.app")


def _service(database: Database) -> ScheduleSettingsService:
    return ScheduleSettingsService(lambda: RepositoryUnitOfWork(database))


def _result(trigger: RunTrigger = RunTrigger.SCHEDULED) -> UpdateResult:
    now = datetime(2026, 7, 20, 1, 0, tzinfo=UTC)
    return UpdateResult(
        crawl_run_id=1,
        status=CrawlStatus.SUCCESS,
        started_at=now,
        finished_at=now,
        source_total=0,
        source_success=0,
        source_failed=0,
        discovered_count=0,
        new_count=0,
        updated_count=0,
        skipped_count=0,
        unclassified_count=0,
        error_summary=None,
        source_results=(),
        trigger=trigger,
    )


def test_default_is_disabled_and_uses_system_iana_timezone(database: Database) -> None:
    settings = _service(database).get()

    assert settings.enabled is False
    assert settings.days == tuple(Weekday)
    assert settings.timezone
    assert ZoneInfo(settings.timezone)
    assert next_scheduled_run(settings, datetime.now(UTC)) is None


def test_settings_enable_disable_and_restart_recovery(database: Database) -> None:
    first = _service(database)
    saved = first.save(
        enabled=True,
        hour=9,
        minute=25,
        days=(Weekday.MON, Weekday.WED, Weekday.FRI),
        timezone="Asia/Shanghai",
    )
    recovered = _service(database).get()

    assert recovered == saved
    assert recovered.days == (Weekday.MON, Weekday.WED, Weekday.FRI)
    disabled = first.save(
        enabled=False,
        hour=recovered.hour,
        minute=recovered.minute,
        days=recovered.days,
        timezone=recovered.timezone,
    )
    assert disabled.enabled is False


@pytest.mark.parametrize("value", ["9:00", "24:00", "09:60", "aa:bb", "09:000"])
def test_invalid_time_is_rejected(value: str) -> None:
    with pytest.raises(ScheduleValidationError):
        parse_time(value)


def test_invalid_days_timezone_and_failed_save_preserve_settings(database: Database) -> None:
    service = _service(database)
    original = service.get()

    with pytest.raises(ScheduleValidationError):
        parse_weekdays(())
    with pytest.raises(ScheduleValidationError):
        service.save(
            enabled=True,
            hour=8,
            minute=0,
            days=(Weekday.MON,),
            timezone="Not/A-Timezone",
        )
    assert service.get() == original


def test_next_run_handles_same_day_midnight_and_week_boundary(database: Database) -> None:
    service = _service(database)
    monday = service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON,),
        timezone="UTC",
    )

    assert next_scheduled_run(monday, datetime(2026, 7, 20, 8, 59, tzinfo=UTC)) == datetime(
        2026, 7, 20, 9, 0, tzinfo=UTC
    )
    assert next_scheduled_run(monday, datetime(2026, 7, 20, 9, 0, tzinfo=UTC)) == datetime(
        2026, 7, 27, 9, 0, tzinfo=UTC
    )

    midnight = service.save(
        enabled=True,
        hour=0,
        minute=5,
        days=(Weekday.TUE,),
        timezone="Asia/Shanghai",
    )
    assert next_scheduled_run(midnight, datetime(2026, 7, 20, 16, 4, tzinfo=UTC)) == datetime(
        2026, 7, 20, 16, 5, tzinfo=UTC
    )


def test_next_run_handles_year_boundary_timezone_change_and_requires_aware_time(
    database: Database,
) -> None:
    service = _service(database)
    shanghai = service.save(
        enabled=True,
        hour=0,
        minute=5,
        days=(Weekday.FRI,),
        timezone="Asia/Shanghai",
    )
    assert next_scheduled_run(shanghai, datetime(2026, 12, 31, 16, 4, tzinfo=UTC)) == datetime(
        2026, 12, 31, 16, 5, tzinfo=UTC
    )

    new_york = service.save(
        enabled=True,
        hour=0,
        minute=5,
        days=(Weekday.FRI,),
        timezone="America/New_York",
    )
    assert next_scheduled_run(new_york, datetime(2026, 12, 31, 16, 4, tzinfo=UTC)) == datetime(
        2027, 1, 1, 5, 5, tzinfo=UTC
    )
    with pytest.raises(ScheduleValidationError, match="明确时区"):
        next_scheduled_run(new_york, datetime(2026, 12, 31, 16, 4))


def test_dst_nonexistent_time_is_skipped_and_ambiguous_time_uses_later_fold(
    database: Database,
) -> None:
    service = _service(database)
    spring = service.save(
        enabled=True,
        hour=2,
        minute=30,
        days=(Weekday.SUN,),
        timezone="America/New_York",
    )
    assert next_scheduled_run(spring, datetime(2026, 3, 8, 0, 0, tzinfo=UTC)) == datetime(
        2026, 3, 15, 6, 30, tzinfo=UTC
    )

    autumn = service.save(
        enabled=True,
        hour=1,
        minute=30,
        days=(Weekday.SUN,),
        timezone="America/New_York",
    )
    assert next_scheduled_run(autumn, datetime(2026, 11, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )

    no_dst = service.save(
        enabled=True,
        hour=1,
        minute=30,
        days=(Weekday.SUN,),
        timezone="Asia/Tokyo",
    )
    assert next_scheduled_run(no_dst, datetime(2026, 11, 1, 0, 0, tzinfo=UTC)) == datetime(
        2026, 11, 7, 16, 30, tzinfo=UTC
    )


def test_system_timezone_rejects_ambiguous_abbreviation_and_falls_back_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TZ", "EST")
    monkeypatch.setattr(schedule_module, "get_localzone_name", lambda: "CST")

    assert system_timezone_name() == "UTC"

    monkeypatch.delenv("TZ")
    monkeypatch.setattr(
        schedule_module,
        "get_localzone_name",
        lambda: (_ for _ in ()).throw(RuntimeError("local timezone unavailable")),
    )
    assert system_timezone_name() == "UTC"


def test_concurrent_first_save_keeps_singleton_and_does_not_fail(database: Database) -> None:
    barrier = Barrier(2)

    def save(hour: int) -> int:
        service = _service(database)
        barrier.wait(timeout=2)
        return service.save(
            enabled=True,
            hour=hour,
            minute=0,
            days=(Weekday.MON,),
            timezone="UTC",
        ).hour

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, (8, 9)))

    assert sorted(results) == [8, 9]
    with RepositoryUnitOfWork(database) as uow:
        assert len(uow.schedule_settings.list()) == 1
        assert uow.schedule_settings.get_singleton().schedule_hour in {8, 9}  # type: ignore[union-attr]


def test_failed_settings_transaction_rolls_back_without_creating_singleton(
    database: Database,
) -> None:
    class FailingCommitUnitOfWork(RepositoryUnitOfWork):
        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            if exception_type is None:
                failure = RuntimeError("commit failed")
                super().__exit__(RuntimeError, failure, None)
                raise failure
            super().__exit__(exception_type, exception, traceback)

    service = ScheduleSettingsService(lambda: FailingCommitUnitOfWork(database))
    with pytest.raises(RuntimeError, match="commit failed"):
        service.save(
            enabled=True,
            hour=9,
            minute=0,
            days=(Weekday.MON,),
            timezone="UTC",
        )

    with RepositoryUnitOfWork(database) as uow:
        assert uow.schedule_settings.list() == []


class ControlledClock:
    def __init__(self, now: datetime) -> None:
        self.current = now
        self.targets: list[datetime] = []
        self.waiting = asyncio.Event()
        self._fire = asyncio.Event()
        self.resume_at: datetime | None = None

    def now(self) -> datetime:
        return self.current

    async def wait_until(self, target: datetime, wake_event: asyncio.Event) -> bool:
        self.targets.append(target)
        self._fire.clear()
        self.waiting.set()
        fire_task = asyncio.create_task(self._fire.wait())
        wake_task = asyncio.create_task(wake_event.wait())
        done, pending = await asyncio.wait(
            (fire_task, wake_task), return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if wake_task in done:
            return True
        self.current = self.resume_at or target
        self.resume_at = None
        return False

    async def advance_to_target(self, *, resume_at: datetime | None = None) -> datetime:
        await asyncio.wait_for(self.waiting.wait(), timeout=1)
        self.waiting.clear()
        target = self.targets[-1]
        self.resume_at = resume_at
        self._fire.set()
        return target


class RecordingRunner:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.called = asyncio.Event()

    async def try_scheduled_update(
        self, *, before_update: Callable[[], None] | None = None
    ) -> UpdateResult | None:
        self.calls += 1
        self.called.set()
        if before_update is not None:
            before_update()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("pipeline failed")
        return _result()


@pytest.mark.asyncio
async def test_scheduler_triggers_without_catchup_or_same_minute_duplicate(
    database: Database,
) -> None:
    service = _service(database)
    service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON,),
        timezone="UTC",
    )
    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    runner = RecordingRunner()
    scheduler = SchedulerService(service, runner, clock=clock)

    await scheduler.start()
    first_target = await clock.advance_to_target()
    await asyncio.wait_for(runner.called.wait(), timeout=1)
    runner.called.clear()
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)

    assert first_target == datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    assert runner.calls == 1
    assert clock.targets[-1] == datetime(2026, 7, 27, 9, 0, tzinfo=UTC)
    assert service.get().last_scheduled_trigger_at == first_target
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_skips_stale_target_after_block_and_rechecks_after_clock_rollback(
    database: Database,
) -> None:
    service = _service(database)
    service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=tuple(Weekday),
        timezone="UTC",
    )
    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    runner = RecordingRunner()
    scheduler = SchedulerService(service, runner, clock=clock)
    await scheduler.start()

    await clock.advance_to_target(resume_at=datetime(2026, 7, 22, 10, 0, tzinfo=UTC))
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)
    assert runner.calls == 0
    assert clock.targets[-1] == datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    assert service.get().last_scheduled_trigger_at is None

    await clock.advance_to_target(resume_at=datetime(2026, 7, 23, 8, 30, tzinfo=UTC))
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)
    assert runner.calls == 0
    assert clock.targets[-1] == datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_continues_after_pipeline_error_and_reload_cancels_old_plan(
    database: Database,
) -> None:
    service = _service(database)
    service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON, Weekday.TUE),
        timezone="UTC",
    )
    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    runner = RecordingRunner(fail_first=True)
    scheduler = SchedulerService(service, runner, clock=clock)
    await scheduler.start()

    await clock.advance_to_target()
    await asyncio.wait_for(runner.called.wait(), timeout=1)
    runner.called.clear()
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)
    assert runner.calls == 1
    clock.waiting.clear()

    service.save(
        enabled=True,
        hour=10,
        minute=0,
        days=(Weekday.TUE,),
        timezone="UTC",
    )
    await scheduler.reload()
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)
    assert clock.targets[-1] == datetime(2026, 7, 21, 10, 0, tzinfo=UTC)

    service.save(
        enabled=False,
        hour=10,
        minute=0,
        days=(Weekday.TUE,),
        timezone="UTC",
    )
    await scheduler.reload()
    await asyncio.sleep(0)
    assert scheduler.view().status is SchedulerStatus.DISABLED
    await scheduler.stop()


class BlockingPipeline(UpdatePipeline):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.triggers: list[RunTrigger] = []
        self.fail = False

    async def update(self, **kwargs: object) -> UpdateResult:
        trigger = cast(RunTrigger, kwargs["trigger"])
        self.triggers.append(trigger)
        self.started.set()
        if self.fail:
            self.fail = False
            raise RuntimeError("boom")
        await self.release.wait()
        return _result(trigger)


@pytest.mark.asyncio
async def test_manual_and_scheduled_updates_share_lock_and_release_after_errors(
    database: Database,
) -> None:
    pipeline = BlockingPipeline()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    lock = UpdateLock()
    manual = WebUpdateService(database, context, lock)
    scheduled = UpdateExecutionService(database, context, lock)
    manual_task = asyncio.create_task(manual.update())
    await pipeline.started.wait()
    assert await scheduled.try_scheduled_update() is None
    pipeline.release.set()
    await manual_task

    pipeline.started.clear()
    pipeline.release.clear()
    scheduled_task = asyncio.create_task(scheduled.try_scheduled_update())
    await pipeline.started.wait()
    with pytest.raises(UpdateInProgressError):
        await manual.update()
    pipeline.release.set()
    await scheduled_task
    assert pipeline.triggers == [RunTrigger.MANUAL_WEB, RunTrigger.SCHEDULED]

    pipeline.started.clear()
    pipeline.release.clear()
    pipeline.fail = True
    with pytest.raises(RuntimeError):
        await manual.update()
    pipeline.release.set()
    assert (await manual.update()).status is CrawlStatus.SUCCESS


def test_update_lock_lease_cannot_release_twice_or_replace_current_owner() -> None:
    lock = UpdateLock()
    lease = lock.acquire()

    assert lease is not None
    assert lock.acquire() is None
    assert lock.locked is True
    lease.release()
    with pytest.raises(RuntimeError, match="already been released"):
        lease.release()
    assert lock.locked is False


@pytest.mark.asyncio
async def test_update_lock_lease_cannot_be_released_by_another_task() -> None:
    lock = UpdateLock()
    lease = lock.acquire()
    assert lease is not None

    async def release_from_other_task() -> None:
        lease.release()

    with pytest.raises(RuntimeError, match="only be released by its owner"):
        await asyncio.create_task(release_from_other_task())
    assert lock.locked is True
    lease.release()
    assert lock.locked is False


@pytest.mark.asyncio
async def test_lock_busy_schedule_skips_without_marking_trigger(database: Database) -> None:
    service = _service(database)
    service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON,),
        timezone="UTC",
    )
    pipeline = BlockingPipeline()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    lock = UpdateLock()
    manual = WebUpdateService(database, context, lock)
    scheduled = UpdateExecutionService(database, context, lock)
    manual_task = asyncio.create_task(manual.update())
    await pipeline.started.wait()

    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    scheduler = SchedulerService(service, scheduled, clock=clock)
    await scheduler.start()
    target = await clock.advance_to_target()
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)

    assert service.get().last_scheduled_trigger_at is None
    assert pipeline.triggers == [RunTrigger.MANUAL_WEB]
    assert clock.targets[-1] > target

    pipeline.release.set()
    await manual_task
    await scheduler.stop()


@pytest.mark.asyncio
async def test_pipeline_startup_failure_is_marked_once_after_lock_acquisition(
    database: Database,
) -> None:
    service = _service(database)
    service.save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON,),
        timezone="UTC",
    )

    @asynccontextmanager
    async def failing_context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        raise RuntimeError("pipeline startup failed")
        yield cast(UpdatePipeline, None)

    execution = UpdateExecutionService(database, failing_context, UpdateLock())
    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    scheduler = SchedulerService(service, execution, clock=clock)
    await scheduler.start()
    target = await clock.advance_to_target()
    await asyncio.wait_for(clock.waiting.wait(), timeout=1)

    assert service.get().last_scheduled_trigger_at == target
    assert clock.targets[-1] > target
    await scheduler.stop()


def test_scheduled_crawl_run_records_trigger(database: Database) -> None:
    run_id = CrawlRunService(lambda: RepositoryUnitOfWork(database)).start(
        source_total=0, trigger=RunTrigger.SCHEDULED
    )
    with RepositoryUnitOfWork(database) as uow:
        assert uow.crawl_runs.get(run_id).trigger is RunTrigger.SCHEDULED  # type: ignore[union-attr]


def test_invalid_crawl_run_trigger_is_rejected_and_all_labels_are_rendered(
    database: Database,
) -> None:
    service = CrawlRunService(lambda: RepositoryUnitOfWork(database))
    for trigger in RunTrigger:
        service.start(source_total=0, trigger=trigger)

    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, status, trigger, source_total, source_success, source_failed, "
                    "discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES "
                    "('2026-07-19 00:00:00', 'running', 'invalid', 0, 0, 0, 0, 0, 0, 0, 0)"
                )
            )

    application = create_app(database=database, enforce_migrations=False)
    with TestClient(application) as client:
        page = client.get("/runs")
    assert page.status_code == 200
    for label in ("历史手动", "网页手动", "CLI 手动", "定时"):
        assert label in page.text


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_stops_enabled_scheduler_with_controlled_time(
    database: Database,
) -> None:
    _service(database).save(
        enabled=True,
        hour=9,
        minute=0,
        days=(Weekday.MON,),
        timezone="UTC",
    )
    clock = ControlledClock(datetime(2026, 7, 20, 8, 0, tzinfo=UTC))
    pipeline = BlockingPipeline()
    pipeline.release.set()

    @asynccontextmanager
    async def context(_database: Database) -> AsyncGenerator[UpdatePipeline]:
        yield pipeline

    application = create_app(
        database=database,
        enforce_migrations=False,
        pipeline_context_factory=context,
        scheduler_clock=clock,
    )
    async with application.router.lifespan_context(application):
        await clock.advance_to_target()
        await asyncio.wait_for(pipeline.started.wait(), timeout=1)
        assert pipeline.triggers == [RunTrigger.SCHEDULED]

    assert application.state.services.scheduler.view().status is SchedulerStatus.STOPPED


@pytest.mark.asyncio
async def test_fastapi_startup_failure_closes_services_and_owned_database(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed = False

    def fake_from_settings(_cls: type[Database], _settings: object = None) -> Database:
        return database

    def record_dispose() -> None:
        nonlocal disposed
        disposed = True
        database.engine.dispose()

    def fail_migration_check(_database: Database) -> None:
        raise RuntimeError("migration check failed")

    monkeypatch.setattr(Database, "from_settings", classmethod(fake_from_settings))
    monkeypatch.setattr(database, "dispose", record_dispose)
    monkeypatch.setattr(web_app_module, "require_current_migration", fail_migration_check)
    application = create_app(enforce_migrations=True)

    with pytest.raises(RuntimeError, match="migration check failed"):
        async with application.router.lifespan_context(application):
            pass

    assert disposed is True


def test_web_settings_page_save_validation_and_immediate_display(database: Database) -> None:
    application = create_app(database=database, enforce_migrations=False)
    with TestClient(application, raise_server_exceptions=False) as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "应用必须保持运行" in page.text
        assert "设置" in client.get("/").text
        with database.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM schedule_settings")) == 0

        invalid = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "09:00",
                "days": "mon",
                "timezone": "Invalid/Zone",
            },
        )
        assert invalid.status_code == 400
        assert application.state.services.schedule_settings.get().enabled is False

        saved = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "09:30",
                "days": ["mon", "fri"],
                "timezone": "Asia/Shanghai",
            },
            follow_redirects=True,
        )
        assert saved.status_code == 200
        assert "设置已保存并立即生效" in saved.text
        assert "Asia/Shanghai" in saved.text
        assert "下一次计划运行" in saved.text


def test_web_settings_page_recovers_from_deleted_or_invalid_persisted_timezone(
    database: Database,
) -> None:
    with RepositoryUnitOfWork(database) as uow:
        uow.schedule_settings.add(
            ScheduleSettings(
                id=1,
                schedule_enabled=True,
                schedule_hour=9,
                schedule_minute=0,
                schedule_days_mask=127,
                timezone="Deleted/Timezone",
                updated_at=datetime.now(UTC),
            )
        )
    application = create_app(database=database, enforce_migrations=False)
    with TestClient(application, raise_server_exceptions=False) as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "当前时区不可用" in page.text
        recovered = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "09:00",
                "days": "mon",
                "timezone": "UTC",
            },
            follow_redirects=True,
        )
        assert recovered.status_code == 200
        assert "设置已保存并立即生效" in recovered.text


def test_web_reports_committed_settings_when_scheduler_reload_fails_and_can_recover(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(database=database, enforce_migrations=False)
    scheduler = application.state.services.scheduler
    original_reload = scheduler.reload

    async def fail_reload() -> None:
        raise RuntimeError("internal reload detail")

    with TestClient(application, raise_server_exceptions=False) as client:
        monkeypatch.setattr(scheduler, "reload", fail_reload)
        failed = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "07:45",
                "days": ["tue", "thu"],
                "timezone": "Asia/Shanghai",
            },
        )
        assert failed.status_code == 503
        assert "设置已保存, 但调度器未能立即重载" in failed.text
        assert "internal reload detail" not in failed.text
        assert application.state.services.schedule_settings.get().hour == 7

        monkeypatch.setattr(scheduler, "reload", original_reload)
        recovered = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "07:45",
                "days": ["tue", "thu"],
                "timezone": "Asia/Shanghai",
            },
            follow_redirects=True,
        )
        assert recovered.status_code == 200
        assert "设置已保存并立即生效" in recovered.text


def test_web_does_not_reload_scheduler_when_settings_save_fails(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_app(database=database, enforce_migrations=False)
    reload_calls = 0

    def fail_save(**_kwargs: object) -> None:
        raise RuntimeError("commit failed")

    async def record_reload() -> None:
        nonlocal reload_calls
        reload_calls += 1

    monkeypatch.setattr(application.state.services.schedule_settings, "save", fail_save)
    monkeypatch.setattr(application.state.services.scheduler, "reload", record_reload)
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/settings",
            data={
                "enabled": "true",
                "schedule_time": "09:00",
                "days": "mon",
                "timezone": "UTC",
            },
        )

    assert response.status_code == 500
    assert reload_calls == 0


def test_cli_schedule_show_enable_disable_and_invalid_input(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda: None)

    def fake_from_settings(_cls: type[Database]) -> Database:
        return database

    monkeypatch.setattr(cli.Database, "from_settings", classmethod(fake_from_settings))

    assert cli.main(["schedule", "show"]) == 0
    assert "enabled=false" in capsys.readouterr().out
    assert (
        cli.main(
            [
                "schedule",
                "enable",
                "--time",
                "08:15",
                "--days",
                "mon,tue,wed,thu,fri",
                "--timezone",
                "Asia/Shanghai",
            ]
        )
        == 0
    )
    assert "enabled=true" in capsys.readouterr().out
    assert cli.main(["schedule", "disable"]) == 0
    assert "enabled=false" in capsys.readouterr().out
    assert cli.main(["schedule", "enable", "--time", "25:00", "--days", "mon"]) == 2
    assert "error:" in capsys.readouterr().err
