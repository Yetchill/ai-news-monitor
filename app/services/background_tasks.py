"""Lifecycle-owned asyncio tasks for lightweight in-process background work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

LOGGER = logging.getLogger(__name__)


class BackgroundTaskManager:
    """Keep strong task references, report failures, and drain work on shutdown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[object]] = set()
        self._accepting = True

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def start(self, coroutine: Coroutine[Any, Any, object], *, name: str) -> asyncio.Task[object]:
        if not self._accepting:
            coroutine.close()
            raise RuntimeError("后台任务管理器正在关闭, 无法接受新任务。")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task[object]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("Background task failed: %s", task.get_name())

    async def aclose(self) -> None:
        self._accepting = False
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
