"""Lifecycle owner for the packaged desktop application."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import uvicorn

from app.config import Settings
from app.services.scheduler_service import SchedulerClock
from app.services.single_instance import SingleInstanceError, SingleInstanceLock
from app.utils.logging import configure_logging
from app.web.app import create_app

LOGGER = logging.getLogger(__name__)
LOOPBACK_HOST = "127.0.0.1"


class DesktopShutdownError(RuntimeError):
    """The Web server still owns process resources after forced shutdown."""


class Tray(Protocol):
    """Small surface needed from a platform tray implementation."""

    def run(self) -> None: ...

    def stop(self) -> None: ...


class DesktopActions(Protocol):
    """Commands exposed by the native tray menu."""

    def open_page(self) -> None: ...

    def update_now(self) -> None: ...

    def open_logs(self) -> None: ...

    def exit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class DesktopRuntimeOptions:
    open_browser: bool = True
    startup_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 15.0
    shutdown_file: Path | None = None
    scheduler_clock: SchedulerClock | None = None


class PackagedTestSchedulerClock:
    """Advance one persisted schedule target per short test-only wall-clock interval."""

    def __init__(self, interval_seconds: float, *, current: datetime | None = None) -> None:
        if not 0.1 <= interval_seconds <= 60.0:
            raise ValueError("test scheduler interval must be between 0.1 and 60 seconds")
        self._interval_seconds = interval_seconds
        self._current = current or datetime.now(UTC)

    def now(self) -> datetime:
        return self._current

    async def wait_until(self, target: datetime, wake_event: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=self._interval_seconds)
            return True
        except TimeoutError:
            self._current = target
            return False


@dataclass(frozen=True, slots=True)
class InstanceState:
    pid: int
    port: int

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}/"


class InstanceStateStore:
    """Publish the loopback address owned by the process holding the data lock."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> InstanceState | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            pid = payload["pid"]
            port = payload["port"]
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                return None
            return InstanceState(pid=pid, port=port)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def write(self, state: InstanceState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({"pid": state.pid, "port": state.port}),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def clear_if_owned(self, pid: int) -> None:
        state = self.read()
        if state is None or state.pid != pid:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class DesktopRuntime(DesktopActions):
    """Run Uvicorn, the browser, and the tray as one desktop lifecycle."""

    def __init__(
        self,
        settings: Settings,
        *,
        options: DesktopRuntimeOptions | None = None,
        browser_open: Callable[[str], object] = webbrowser.open,
        tray_factory: Callable[[DesktopActions], Tray] | None = None,
    ) -> None:
        self.settings = settings
        self.options = options or DesktopRuntimeOptions()
        self._browser_open = browser_open
        self._tray_factory = tray_factory or _default_tray_factory
        self._state_store = InstanceStateStore(settings.data_dir / "desktop-instance.json")
        self._instance_lock = SingleInstanceLock(settings.database_url)
        self._server: uvicorn.Server | None = None
        self._server_thread: threading.Thread | None = None
        self._tray: Tray | None = None
        self._url: str | None = None
        self._exit_requested = threading.Event()

    def run(self) -> bool:
        """Run until tray exit; return false when an existing instance was activated."""

        configure_logging(self.settings)
        try:
            self._instance_lock.acquire()
        except SingleInstanceError:
            self._activate_existing_instance()
            return False

        listener: socket.socket | None = None
        pid = os.getpid()
        try:
            listener = _bind_loopback_socket()
            port = listener.getsockname()[1]
            state = InstanceState(pid=pid, port=port)
            self._url = state.url
            self._state_store.write(state)

            application = create_app(
                settings=self.settings,
                acquire_instance_lock=False,
                scheduler_clock=self.options.scheduler_clock,
            )
            config = uvicorn.Config(
                application,
                host=LOOPBACK_HOST,
                port=port,
                access_log=False,
                log_config=None,
            )
            self._server = uvicorn.Server(config)
            startup_errors: list[BaseException] = []

            def serve() -> None:
                try:
                    assert self._server is not None
                    assert listener is not None
                    self._server.run(sockets=[listener])
                except BaseException as exc:
                    startup_errors.append(exc)
                    LOGGER.exception("Desktop Web server stopped unexpectedly")

            self._server_thread = threading.Thread(
                target=serve,
                name="aim-web-server",
                daemon=True,
            )
            self._server_thread.start()
            _wait_until_ready(
                f"{self._url}healthz",
                self._server_thread,
                startup_errors,
                self.options.startup_timeout_seconds,
            )
            LOGGER.info("Desktop application ready at %s", self._url)
            if self.options.open_browser:
                self.open_page()
            self._tray = self._tray_factory(self)
            self._start_shutdown_file_watcher()
            self._tray.run()
            return True
        finally:
            self._exit_requested.set()
            self._finalize_runtime(pid, listener)

    def open_page(self) -> None:
        if self._url is not None:
            self._browser_open(self._url)

    def update_now(self) -> None:
        if self._url is None:
            return

        def request_update() -> None:
            request = urllib.request.Request(f"{self._url}updates", method="POST")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    response.read(1)
                LOGGER.info("Tray-triggered update completed")
            except Exception:
                LOGGER.exception("Tray-triggered update failed")

        threading.Thread(
            target=request_update,
            name="aim-tray-update",
            daemon=True,
        ).start()

    def open_logs(self) -> None:
        self.settings.log_dir.mkdir(parents=True, exist_ok=True)
        startfile = getattr(os, "startfile", None)
        if callable(startfile):
            startfile(str(self.settings.log_dir))
        else:
            self._browser_open(self.settings.log_dir.resolve().as_uri())

    def exit(self) -> None:
        self._exit_requested.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._tray is not None:
            self._tray.stop()

    def _activate_existing_instance(self) -> None:
        deadline = time.monotonic() + min(2.0, self.options.startup_timeout_seconds)
        state = self._state_store.read()
        while state is None and time.monotonic() < deadline:
            time.sleep(0.05)
            state = self._state_store.read()
        if state is None:
            raise SingleInstanceError(
                "AI Intelligence Monitor 已在运行, 但无法读取现有窗口地址; 请查看日志。"
            )
        LOGGER.info("Activating existing desktop instance at %s", state.url)
        if self.options.open_browser:
            self._browser_open(state.url)

    def _shutdown_server(self) -> None:
        server = self._server
        thread = self._server_thread
        if server is None or thread is None:
            return
        server.should_exit = True
        thread.join(self.options.shutdown_timeout_seconds)
        if thread.is_alive():
            LOGGER.error("Web server did not stop before the graceful shutdown timeout")
            server.force_exit = True
            thread.join(2.0)
            if thread.is_alive():
                LOGGER.critical(
                    "Web server thread survived forced shutdown; retaining the instance lock"
                )
                raise DesktopShutdownError(
                    "本地服务无法安全停止。实例锁将保持到本进程终止, 以保护数据库。"
                )
        LOGGER.info("Desktop Web server stopped cleanly")

    def _finalize_runtime(self, pid: int, listener: socket.socket | None) -> None:
        """Release process ownership only after the server has definitely stopped."""

        self._shutdown_server()
        self._state_store.clear_if_owned(pid)
        self._instance_lock.release()
        if listener is not None:
            listener.close()

    def _start_shutdown_file_watcher(self) -> None:
        shutdown_file = self.options.shutdown_file
        if shutdown_file is None:
            return

        def watch() -> None:
            while not self._exit_requested.wait(0.1):
                if not shutdown_file.is_file():
                    continue
                LOGGER.info("Test shutdown file received at %s", shutdown_file)
                try:
                    shutdown_file.unlink()
                except OSError:
                    LOGGER.warning("Could not consume test shutdown file", exc_info=True)
                self.exit()
                return

        threading.Thread(
            target=watch,
            name="aim-test-shutdown-watcher",
            daemon=True,
        ).start()


def _bind_loopback_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((LOOPBACK_HOST, 0))
        return listener
    except Exception:
        listener.close()
        raise


def _wait_until_ready(
    health_url: str,
    server_thread: threading.Thread,
    startup_errors: list[BaseException],
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if startup_errors:
            raise RuntimeError("本地服务启动失败。") from startup_errors[0]
        if not server_thread.is_alive():
            raise RuntimeError("本地服务在启动完成前退出。")
        try:
            with urllib.request.urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.05)
    raise TimeoutError("本地服务启动超时。")


def _default_tray_factory(actions: DesktopActions) -> Tray:
    from app.desktop.tray import create_tray

    return create_tray(actions)
