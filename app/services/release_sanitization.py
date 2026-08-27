"""Create release-safe SQLite copies without altering the user's live database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url


def create_sanitized_database_copy(database_url: str, destination: Path) -> Path:
    """Backup a SQLite database and erase credentials only in the new copy."""

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("仅支持清理文件型 SQLite 数据库。")
    source = Path(url.database).expanduser().resolve()
    target = destination.expanduser().resolve()
    if source == target:
        raise ValueError("清理副本不能覆盖正在使用的数据库。")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
        tables = {
            row[0]
            for row in target_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "ai_provider_settings" in tables:
            target_connection.execute("UPDATE ai_provider_settings SET api_key='' ")
        if "ai_settings" in tables:
            target_connection.execute("UPDATE ai_settings SET api_key='' ")
        target_connection.commit()
    return target
