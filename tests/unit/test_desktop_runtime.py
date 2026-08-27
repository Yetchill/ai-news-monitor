"""Desktop host lifecycle and Windows release regression tests."""

# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import ctypes
import socket
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from fastapi.testclient import TestClient

import app.storage.migrations.runtime as migration_runtime
from app.config.settings import Settings
from app.desktop.__main__ import (
    _environment_browser_enabled,  # pyright: ignore[reportPrivateUsage]
    _test_mode_enabled,  # pyright: ignore[reportPrivateUsage]
    _test_scheduler_clock_from_environment,  # pyright: ignore[reportPrivateUsage]
    _test_shutdown_file_from_environment,  # pyright: ignore[reportPrivateUsage]
)
from app.desktop.runtime import (
    DesktopActions,
    DesktopRuntime,
    DesktopRuntimeOptions,
    DesktopShutdownError,
    InstanceState,
    InstanceStateStore,
)
from app.desktop.tray import _Win32Api  # pyright: ignore[reportPrivateUsage]
from app.services.single_instance import SingleInstanceError, SingleInstanceLock
from app.storage.database import Database
from app.storage.migrations.runtime import backup_sqlite_before_migration
from app.web.app import create_app


class ExitImmediatelyTray:
    def __init__(self, actions: DesktopActions) -> None:
        self.actions = actions
        self.stopped = False

    def run(self) -> None:
        self.actions.exit()

    def stop(self) -> None:
        self.stopped = True


class BlockingTray:
    def __init__(self, _actions: DesktopActions) -> None:
        self.stopped = threading.Event()

    def run(self) -> None:
        assert self.stopped.wait(10)

    def stop(self) -> None:
        self.stopped.set()


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "user-data"
    return Settings(
        data_dir=data_dir,
        database_url=f"sqlite:///{(data_dir / 'intelligence.db').as_posix()}",
        log_dir=data_dir / "logs",
        output_dir=data_dir / "output",
        environment="test",
        _env_file=None,  # pyright: ignore[reportCallIssue]
    )


def test_health_endpoint_reports_readiness(database: Database) -> None:
    application = create_app(database=database, enforce_migrations=False)
    with TestClient(application) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_desktop_runtime_uses_random_loopback_port_and_can_skip_browser(tmp_path: Path) -> None:
    opened: list[str] = []
    runtime = DesktopRuntime(
        _settings(tmp_path),
        options=DesktopRuntimeOptions(
            open_browser=False,
            startup_timeout_seconds=10,
            shutdown_timeout_seconds=10,
        ),
        browser_open=opened.append,
        tray_factory=ExitImmediatelyTray,
    )

    assert runtime.run() is True
    assert opened == []
    assert not (tmp_path / "user-data" / "desktop-instance.json").exists()
    assert (tmp_path / "user-data" / "intelligence.db").is_file()


def test_second_desktop_start_opens_first_instance_without_starting_server(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = InstanceStateStore(settings.data_dir / "desktop-instance.json")
    store.write(InstanceState(pid=1234, port=32123))
    first = SingleInstanceLock(settings.database_url)
    first.acquire()
    opened: list[str] = []
    tray_created = False

    def tray_factory(actions: DesktopActions) -> ExitImmediatelyTray:
        nonlocal tray_created
        tray_created = True
        return ExitImmediatelyTray(actions)

    try:
        runtime = DesktopRuntime(
            settings,
            browser_open=opened.append,
            tray_factory=tray_factory,
        )
        assert runtime.run() is False
    finally:
        first.release()

    assert opened == ["http://127.0.0.1:32123/"]
    assert tray_created is False


def test_guarded_shutdown_file_drives_graceful_desktop_exit(tmp_path: Path) -> None:
    shutdown_file = tmp_path / "request-shutdown"
    runtime = DesktopRuntime(
        _settings(tmp_path),
        options=DesktopRuntimeOptions(
            open_browser=False,
            startup_timeout_seconds=10,
            shutdown_timeout_seconds=10,
            shutdown_file=shutdown_file,
        ),
        tray_factory=BlockingTray,
    )
    result: list[bool] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(runtime.run())
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    state_path = tmp_path / "user-data" / "desktop-instance.json"
    deadline = time.monotonic() + 10
    while not state_path.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state_path.is_file()
    shutdown_file.touch()
    thread.join(10)

    assert not thread.is_alive()
    assert failure == []
    assert result == [True]
    assert not shutdown_file.exists()
    assert not state_path.exists()


def test_stuck_server_retains_instance_lock_and_runtime_state(tmp_path: Path) -> None:
    class StubbornServer:
        should_exit = False
        force_exit = False

    class StubbornThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float] = []

        def join(self, timeout: float | None = None) -> None:
            if timeout is not None:
                self.join_timeouts.append(timeout)

        def is_alive(self) -> bool:
            return True

    class TrackingSocket:
        closed = False

        def close(self) -> None:
            self.closed = True

    settings = _settings(tmp_path)
    runtime = DesktopRuntime(
        settings,
        options=DesktopRuntimeOptions(shutdown_timeout_seconds=0.01),
    )
    server = StubbornServer()
    thread = StubbornThread()
    listener = TrackingSocket()
    runtime._server = cast("uvicorn.Server", server)
    runtime._server_thread = cast(threading.Thread, thread)
    runtime._instance_lock.acquire()
    runtime._state_store.write(InstanceState(pid=9876, port=32123))
    contender = SingleInstanceLock(settings.database_url)
    try:
        with pytest.raises(DesktopShutdownError, match="无法安全停止"):
            runtime._finalize_runtime(9876, cast("socket.socket", listener))

        with pytest.raises(SingleInstanceError):
            contender.acquire()
        assert server.should_exit is True
        assert server.force_exit is True
        assert thread.join_timeouts == [0.01, 2.0]
        assert listener.closed is False
        assert (settings.data_dir / "desktop-instance.json").is_file()
    finally:
        contender.release()
        runtime._instance_lock.release()
        runtime._state_store.clear_if_owned(9876)


def test_shutdown_file_environment_requires_explicit_test_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shutdown_file = tmp_path / "shutdown"
    monkeypatch.setenv("AIM_DESKTOP_SHUTDOWN_FILE", str(shutdown_file))
    monkeypatch.delenv("AIM_DESKTOP_TEST_MODE", raising=False)
    assert _test_shutdown_file_from_environment() is None

    monkeypatch.setenv("AIM_DESKTOP_TEST_MODE", "1")
    assert _test_mode_enabled() is True
    assert _test_shutdown_file_from_environment() == shutdown_file


def test_accelerated_scheduler_clock_requires_explicit_test_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS", "0.1")
    monkeypatch.delenv("AIM_DESKTOP_TEST_MODE", raising=False)
    assert _test_scheduler_clock_from_environment() is None

    monkeypatch.setenv("AIM_DESKTOP_TEST_MODE", "1")
    clock = _test_scheduler_clock_from_environment()
    assert clock is not None
    target = datetime.now(UTC) + timedelta(days=1)

    async def advance() -> bool:
        return await clock.wait_until(target, asyncio.Event())

    assert asyncio.run(advance()) is False
    assert clock.now() == target


def test_accelerated_scheduler_clock_rejects_invalid_guarded_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIM_DESKTOP_TEST_MODE", "1")
    monkeypatch.setenv("AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS", "not-a-number")
    with pytest.raises(ValueError, match="必须是数字"):
        _test_scheduler_clock_from_environment()


def test_browser_environment_switch_can_disable_automatic_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIM_DESKTOP_OPEN_BROWSER", "off")
    assert _environment_browser_enabled() is False
    monkeypatch.setenv("AIM_DESKTOP_OPEN_BROWSER", "1")
    assert _environment_browser_enabled() is True


def test_win32_ctypes_structures_keep_one_process_wide_type_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFunction:
        argtypes: object = None
        restype: object = None

        def __call__(self, *_args: object) -> int:
            return 1

    class FakeLibrary:
        def __init__(self) -> None:
            self.functions: dict[str, FakeFunction] = {}

        def __getattr__(self, name: str) -> FakeFunction:
            return self.functions.setdefault(name, FakeFunction())

    class FakeWindll:
        user32 = FakeLibrary()
        shell32 = FakeLibrary()
        kernel32 = FakeLibrary()

    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    previous = _Win32Api._cached_types
    _Win32Api._cached_types = None
    try:
        first = _Win32Api()
        second = _Win32Api()
        assert first.window_proc_type is second.window_proc_type
        assert first.window_class_type is second.window_class_type
        assert first.notification_data_type is second.notification_data_type
    finally:
        _Win32Api._cached_types = previous


def test_instance_state_store_rejects_invalid_external_values(tmp_path: Path) -> None:
    path = tmp_path / "instance.json"
    path.write_text('{"pid": true, "port": 90000}', encoding="utf-8")

    assert InstanceStateStore(path).read() is None


def test_sqlite_migration_backup_is_valid_and_keeps_newest_five(tmp_path: Path) -> None:
    database_path = tmp_path / "live.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('kept')")
    url = f"sqlite:///{database_path.as_posix()}"

    backups = [backup_sqlite_before_migration(url) for _ in range(7)]

    retained = sorted((tmp_path / "backups").glob("live.before-migration.*.db"))
    assert len(retained) == 5
    assert backups[-1] in retained
    with sqlite3.connect(retained[-1]) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("kept",)


def test_schema_upgrade_failure_still_leaves_pre_migration_backup(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def old_and_new(_database: Database) -> tuple[str, str]:
        return "old-revision", "new-revision"

    monkeypatch.setattr(
        migration_runtime,
        "current_and_head",
        old_and_new,
    )

    def fail_upgrade(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(migration_runtime.command, "upgrade", fail_upgrade)
    with pytest.raises(RuntimeError, match="数据库升级失败"):
        migration_runtime.ensure_database_current(database)

    database_path = Path(database.database_url.removeprefix("sqlite:///"))
    assert len(list((database_path.parent / "backups").glob("test.before-migration.*.db"))) == 1
