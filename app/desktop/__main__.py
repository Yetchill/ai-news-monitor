"""Windowed desktop executable entry point."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from app.config import get_settings
from app.desktop.notifications import show_startup_error
from app.desktop.runtime import (
    DesktopRuntime,
    DesktopRuntimeOptions,
    PackagedTestSchedulerClock,
)

LOGGER = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Intelligence Monitor desktop host")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start in the notification area without opening a browser",
    )
    arguments = parser.parse_args(argv)
    settings = None
    try:
        settings = get_settings()
        open_browser = not arguments.no_browser and _environment_browser_enabled()
        runtime = DesktopRuntime(
            settings,
            options=DesktopRuntimeOptions(
                open_browser=open_browser,
                shutdown_file=_test_shutdown_file_from_environment(),
                scheduler_clock=_test_scheduler_clock_from_environment(),
            ),
        )
        runtime.run()
    except Exception as exc:
        LOGGER.exception("Desktop application startup failed")
        log_path = (
            settings.log_dir / "application.log" if settings is not None else "application.log"
        )
        message = f"应用无法启动: {exc}\n\n详细日志: {log_path}"
        # A modal dialog would hide the original failure and block a headless
        # packaged test. Production startup still shows the Chinese notice.
        if not _test_mode_enabled():
            show_startup_error(message)
        return 1
    return 0


def _environment_browser_enabled() -> bool:
    value = os.environ.get("AIM_DESKTOP_OPEN_BROWSER", "1").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def _test_shutdown_file_from_environment() -> Path | None:
    """Enable packaged smoke-test shutdown only under an explicit test guard."""

    if not _test_mode_enabled():
        return None
    value = os.environ.get("AIM_DESKTOP_SHUTDOWN_FILE", "").strip()
    return Path(value).expanduser().resolve() if value else None


def _test_scheduler_clock_from_environment() -> PackagedTestSchedulerClock | None:
    """Accelerate persisted schedules only for an explicitly guarded packaged test."""

    if not _test_mode_enabled():
        return None
    value = os.environ.get("AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS", "").strip()
    if not value:
        return None
    try:
        interval = float(value)
    except ValueError as exc:
        raise ValueError("AIM_DESKTOP_TEST_SCHEDULER_INTERVAL_SECONDS 必须是数字。") from exc
    return PackagedTestSchedulerClock(interval)


def _test_mode_enabled() -> bool:
    return os.environ.get("AIM_DESKTOP_TEST_MODE", "").strip() == "1"


if __name__ == "__main__":
    raise SystemExit(main())
