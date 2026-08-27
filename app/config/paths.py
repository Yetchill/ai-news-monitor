"""Centralized writable-data and bundled-resource path resolution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRECTORY_NAME = "AIIntelligenceMonitor"


def resource_root() -> Path:
    """Return the read-only application resource root, including PyInstaller."""

    bundled = getattr(sys, "_MEIPASS", None)
    if isinstance(bundled, str) and bundled:
        return Path(bundled).resolve()
    return Path(__file__).resolve().parents[2]


def default_data_dir() -> Path:
    """Return the per-user writable application directory for this platform."""

    explicit = os.environ.get("AIM_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIRECTORY_NAME
        return Path.home() / "AppData" / "Local" / APP_DIRECTORY_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRECTORY_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home).expanduser() if xdg_data_home else Path.home() / ".local" / "share"
    return base / APP_DIRECTORY_NAME


def default_database_url() -> str:
    return f"sqlite:///{(default_data_dir() / 'intelligence.db').as_posix()}"
