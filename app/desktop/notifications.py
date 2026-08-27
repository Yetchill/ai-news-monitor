"""Native startup error reporting for a windowed executable."""

from __future__ import annotations

import ctypes
import sys
from typing import Any, cast


def show_startup_error(message: str) -> None:
    """Show an actionable error even when the executable has no console."""

    if sys.platform == "win32":
        windll = cast(Any, getattr(ctypes, "windll"))  # noqa: B009
        windll.user32.MessageBoxW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
        ]
        windll.user32.MessageBoxW.restype = ctypes.c_int
        windll.user32.MessageBoxW(
            None,
            message,
            "AI Intelligence Monitor 启动失败",
            0x00000010,
        )
        return
    print(message, file=sys.stderr)
