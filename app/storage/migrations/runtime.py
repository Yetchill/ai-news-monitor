"""Safe runtime Alembic upgrades for desktop-style application startup."""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect as sqlalchemy_inspect

from app.config.paths import resource_root
from app.domain.models import Base
from app.storage.database import Database

LOGGER = logging.getLogger(__name__)


def alembic_config(database_url: str) -> Config:
    root = resource_root()
    config = Config()
    config.set_main_option("script_location", str(root / "app" / "storage" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def current_and_head(database: Database) -> tuple[str | None, str | None]:
    config = alembic_config(database.database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    with database.engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return current, head


def ensure_database_current(database: Database) -> None:
    """Initialize or upgrade a database in place, preserving user records."""

    current, head = current_and_head(database)
    if current == head:
        return
    if current is None and _matches_current_unversioned_schema(database):
        LOGGER.info("Stamping complete unversioned database schema at %s", head)
        command.stamp(alembic_config(database.database_url), "head")
        return
    if current is None and sqlalchemy_inspect(database.engine).get_table_names():
        raise RuntimeError(
            "检测到没有版本号且结构不完整的旧数据库。为避免覆盖历史数据, "
            "程序已停止自动升级; 请保留数据库并查看日志。"
        )
    LOGGER.info("Upgrading database schema from %s to %s", current or "empty", head)
    try:
        command.upgrade(alembic_config(database.database_url), "head")
    except Exception:
        LOGGER.exception("Database migration failed; existing database was left in place")
        raise RuntimeError(
            "数据库升级失败。历史数据未被删除; 请查看日志中的 migration failed 详情。"
        ) from None
    migrated, expected = current_and_head(database)
    if migrated != expected:
        raise RuntimeError(f"数据库升级未完成: 当前版本 {migrated or '未知'}, 目标 {expected}。")


def _matches_current_unversioned_schema(database: Database) -> bool:
    inspector = sqlalchemy_inspect(database.engine)
    actual_tables = set(inspector.get_table_names())
    expected_tables = set(Base.metadata.tables)
    if not expected_tables <= actual_tables:
        return False
    for table_name, table in Base.metadata.tables.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        expected_columns = {column.name for column in table.columns}
        if not expected_columns <= actual_columns:
            return False
    if database.database_url.startswith("sqlite"):
        with database.engine.connect() as connection:
            definitions = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name IN ('sources','intelligence_items')"
            ).scalars()
            if "irrelevant" not in " ".join(value or "" for value in definitions):
                return False
    return True
