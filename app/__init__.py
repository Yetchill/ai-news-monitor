"""AI intelligence monitor application package."""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    # The distribution metadata is included in the Windows bundle.  Keeping the
    # version in ``pyproject.toml`` avoids a second, drift-prone version string.
    __version__ = version("ai-intelligence-monitor")
except PackageNotFoundError:  # pragma: no cover - only relevant for raw source trees.
    # ``uv run`` can execute an uninstalled source tree.  The exact same
    # canonical value remains in pyproject.toml; bundled builds use metadata.
    __version__ = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
