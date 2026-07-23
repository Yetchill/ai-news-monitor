"""Shared process-local exclusion for manual and scheduled updates."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from threading import Lock, get_ident

from app.domain.enums import RunTrigger
from app.domain.update import SourcePreviewResult, UpdateMode, UpdateResult
from app.services.update_pipeline import UpdatePipeline
from app.storage.database import Database

PipelineContextFactory = Callable[[Database], AbstractAsyncContextManager[UpdatePipeline]]


class UpdateInProgressError(RuntimeError):
    """Raised when another update already owns the process-local lock."""


class UpdateLock:
    def __init__(self) -> None:
        self._state_lock = Lock()
        self._owner: object | None = None

    def acquire(self) -> "UpdateLockLease | None":
        with self._state_lock:
            if self._owner is not None:
                return None
            token = object()
            self._owner = token
            return UpdateLockLease(self, token)

    def release_lease(self, token: object) -> None:
        with self._state_lock:
            if self._owner is not token:
                raise RuntimeError("update lock lease is not the current owner")
            self._owner = None

    @property
    def locked(self) -> bool:
        with self._state_lock:
            return self._owner is not None


class UpdateLockLease:
    def __init__(self, lock: UpdateLock, token: object) -> None:
        self._lock = lock
        self._token = token
        self._owner = _execution_owner()
        self._released = False

    def release(self) -> None:
        if self._released:
            raise RuntimeError("update lock lease has already been released")
        if _execution_owner() != self._owner:
            raise RuntimeError("update lock lease can only be released by its owner")
        self._lock.release_lease(self._token)
        self._released = True


def _execution_owner() -> object:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return task if task is not None else ("thread", get_ident())


class UpdateExecutionService:
    def __init__(
        self,
        database: Database,
        pipeline_context_factory: PipelineContextFactory,
        update_lock: UpdateLock,
    ) -> None:
        self._database = database
        self._pipeline_context_factory = pipeline_context_factory
        self._lock = update_lock

    async def preview(self, source_id: int, *, max_items: int = 10) -> SourcePreviewResult:
        async with self._pipeline_context_factory(self._database) as pipeline:
            return await pipeline.preview(source_id, max_items=max_items)

    async def update(
        self,
        *,
        trigger: RunTrigger,
        source_id: int | None = None,
        allow_disabled: bool = False,
        formal_only: bool = False,
        mode: UpdateMode = UpdateMode.INCREMENTAL,
        max_pages: int | None = None,
        max_items: int | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
    ) -> UpdateResult:
        return await self._execute(
            trigger=trigger,
            source_id=source_id,
            allow_disabled=allow_disabled,
            formal_only=formal_only,
            mode=mode,
            max_pages=max_pages,
            max_items=max_items,
            published_from=published_from,
            published_to=published_to,
        )

    async def _execute(
        self,
        *,
        trigger: RunTrigger,
        source_id: int | None = None,
        allow_disabled: bool = False,
        formal_only: bool = False,
        mode: UpdateMode = UpdateMode.INCREMENTAL,
        max_pages: int | None = None,
        max_items: int | None = None,
        published_from: datetime | None = None,
        published_to: datetime | None = None,
        before_update: Callable[[], None] | None = None,
    ) -> UpdateResult:
        lease = self._lock.acquire()
        if lease is None:
            raise UpdateInProgressError("已有更新正在运行, 请等待完成后再试。")
        try:
            if before_update is not None:
                before_update()
            async with self._pipeline_context_factory(self._database) as pipeline:
                return await pipeline.update(
                    source_id=source_id,
                    allow_disabled=allow_disabled,
                    formal_only=formal_only,
                    mode=mode,
                    max_pages=max_pages,
                    max_items=max_items,
                    published_from=published_from,
                    published_to=published_to,
                    trigger=trigger,
                )
        finally:
            lease.release()

    async def try_scheduled_update(
        self, *, before_update: Callable[[], None] | None = None
    ) -> UpdateResult | None:
        try:
            return await self._execute(
                trigger=RunTrigger.SCHEDULED,
                formal_only=False,
                before_update=before_update,
            )
        except UpdateInProgressError:
            return None
