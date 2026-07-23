"""Lightweight, single-process runtime scheduler."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.domain.scheduling import SchedulerStatus, ScheduleSettingsValue, ScheduleView
from app.domain.update import UpdateResult
from app.services.schedule_settings_service import (
    ScheduleSettingsService,
    ScheduleValidationError,
    next_scheduled_run,
    validate_timezone,
)

LOGGER = logging.getLogger("app.scheduler")


class SchedulerReloadError(RuntimeError):
    """Raised after settings commit when the in-process scheduler could not reload."""


class SchedulerClock(Protocol):
    def now(self) -> datetime: ...

    async def wait_until(self, target: datetime, wake_event: asyncio.Event) -> bool: ...


class ScheduledUpdateRunner(Protocol):
    async def try_scheduled_update(
        self, *, before_update: Callable[[], None] | None = None
    ) -> UpdateResult | None: ...


class AsyncioSchedulerClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def wait_until(self, target: datetime, wake_event: asyncio.Event) -> bool:
        delay = max(0.0, (target - self.now()).total_seconds())
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=delay)
            return True
        except TimeoutError:
            return False


class SchedulerService:
    """Own at most one background task and reload it after committed setting changes."""

    def __init__(
        self,
        settings: ScheduleSettingsService,
        updates: ScheduledUpdateRunner,
        *,
        clock: SchedulerClock | None = None,
    ) -> None:
        self._settings = settings
        self._updates = updates
        self._clock = clock or AsyncioSchedulerClock()
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._status = SchedulerStatus.STOPPED
        self._next_run_at: datetime | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        settings = self._settings.get()
        if not settings.enabled:
            self._status = SchedulerStatus.DISABLED
            self._next_run_at = None
            return
        self._status = SchedulerStatus.WAITING
        self._task = asyncio.create_task(self._run(), name="runtime-scheduler")

    async def reload(self) -> None:
        settings = self._settings.get()
        self._wake_event.set()
        if settings.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="runtime-scheduler")
        elif not settings.enabled:
            self._status = SchedulerStatus.DISABLED
            self._next_run_at = None

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._wake_event.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._status = SchedulerStatus.STOPPED
        self._next_run_at = None

    def view(self) -> ScheduleView:
        settings = self._settings.get()
        try:
            validate_timezone(settings.timezone)
            next_run = next_scheduled_run(settings, self._clock.now()) if settings.enabled else None
        except ScheduleValidationError as exc:
            return ScheduleView(
                settings=settings,
                next_run_at=None,
                status=SchedulerStatus.STOPPED,
                error=str(exc),
            )
        status = self._status if settings.enabled else SchedulerStatus.DISABLED
        return ScheduleView(settings=settings, next_run_at=next_run, status=status)

    async def _run(self) -> None:
        try:
            while True:
                settings = self._settings.get()
                if not settings.enabled:
                    self._status = SchedulerStatus.DISABLED
                    self._next_run_at = None
                    return
                target = next_scheduled_run(settings, self._clock.now())
                if target is None:
                    return
                self._next_run_at = target
                self._status = SchedulerStatus.WAITING
                self._wake_event.clear()
                if await self._clock.wait_until(target, self._wake_event):
                    continue

                now = self._clock.now()
                if now < target:
                    continue
                if now >= target + timedelta(minutes=1):
                    LOGGER.info("Skipped missed scheduled update at %s", target.isoformat())
                    continue

                current = self._settings.get()
                if not current.enabled or _schedule_signature(current) != _schedule_signature(
                    settings
                ):
                    continue
                if (
                    current.last_scheduled_trigger_at is not None
                    and current.last_scheduled_trigger_at >= target
                ):
                    continue
                self._status = SchedulerStatus.RUNNING

                def mark_trigger(scheduled_target: datetime = target) -> None:
                    self._settings.mark_scheduled_trigger(scheduled_target)

                try:
                    result = await self._updates.try_scheduled_update(before_update=mark_trigger)
                    if result is None:
                        LOGGER.info("Scheduled update skipped because another update is running")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Scheduled update failed; scheduler will continue")
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Scheduler loop failed")
            self._status = SchedulerStatus.STOPPED
            self._next_run_at = None


def _schedule_signature(settings: ScheduleSettingsValue) -> tuple[object, ...]:
    return (
        settings.enabled,
        settings.hour,
        settings.minute,
        settings.days,
        settings.timezone,
        settings.updated_at,
    )
