# -*- mode: python ; coding: utf-8 -*-
"""Reproducible Windows onedir build for AI Intelligence Monitor.

The distribution version comes from ``pyproject.toml``. Do not add a second
version constant here or in the Inno Setup script.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


PROJECT_ROOT = Path(SPECPATH).resolve().parent
DESKTOP_ENTRY = PROJECT_ROOT / "app" / "desktop" / "__main__.py"
WEB_ENTRY = PROJECT_ROOT / "app" / "web" / "__main__.py"
ENTRY_SCRIPT = DESKTOP_ENTRY if DESKTOP_ENTRY.exists() else WEB_ENTRY

# Dynamic resource lookup, Alembic migration discovery, package metadata, CA
# certificates, exporters, and the optional native tray all need an explicit
# collection rule. Keep the requirements visible here so artifact inspection
# can assert their presence after every Windows build.
datas = [(str(PROJECT_ROOT / "alembic.ini"), ".")]
datas += collect_data_files(
    "app",
    include_py_files=True,
    includes=[
        "config/*.yaml",
        "config/classification/*.yaml",
        "web/templates/*.html",
        "web/static/*",
        "storage/migrations/*.py",
        "storage/migrations/versions/*.py",
        "storage/migrations/README",
    ],
)
datas += copy_metadata("ai-intelligence-monitor")

try:
    import certifi

    datas.append((certifi.where(), "certifi"))
except ImportError:
    raise SystemExit("certifi must be installed before packaging")

hiddenimports = [
    *collect_submodules("app.exporters"),
    *collect_submodules("app.storage.migrations"),
    "certifi",
]

# ``app.desktop.tray`` is an optional native shell during source development,
# but must be included when it exists for the packaged Windows application.
if importlib.util.find_spec("app.desktop") is not None:
    hiddenimports.extend(collect_submodules("app.desktop"))
if importlib.util.find_spec("app.desktop.tray") is not None:
    hiddenimports.append("app.desktop.tray")

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pyright", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI 情报助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AIIntelligenceMonitor",
)
