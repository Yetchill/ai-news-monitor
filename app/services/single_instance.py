"""Cross-platform advisory lock guarding one writable application database."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import make_url


class SingleInstanceError(RuntimeError):
    """Another process already owns the application's data directory."""


class SingleInstanceLock:
    def __init__(self, database_url: str) -> None:
        url = make_url(database_url)
        database = url.database if url.get_backend_name() == "sqlite" else None
        self.path = (
            Path(database).expanduser().resolve().with_suffix(".lock")
            if database and database != ":memory:"
            else None
        )
        self._handle: BinaryIO | None = None

    def acquire(self) -> None:
        if self.path is None or self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                _ensure_lock_byte(handle)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, PermissionError) as exc:
            handle.close()
            raise SingleInstanceError(
                "AI Intelligence Monitor 已在运行; 请先关闭已有窗口后再启动。"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _ensure_lock_byte(handle: BinaryIO) -> None:
    """Initialize the byte locked by msvcrt without growing an existing file."""

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
