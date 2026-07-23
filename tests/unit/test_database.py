"""Database initialization and constraint tests."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.domain.enums import Category, SourceKind
from app.domain.models import IntelligenceItem, Source
from app.services.content_admission import ContentAdmissionPolicy
from app.services.source_seed_service import SourceSeedService
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import require_current_migration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_sqlite_foreign_keys_are_enabled_in_modern_transaction_mode(database: Database) -> None:
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_alembic_initializes_database(tmp_path: Path) -> None:
    database_path = tmp_path / "migrated.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")

    command.upgrade(config, "head")

    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(database.engine)
        assert set(inspector.get_table_names()) == {
            "ai_jobs",
            "ai_settings",
            "alembic_version",
            "crawl_runs",
            "crawl_source_executions",
            "intelligence_items",
            "item_review_events",
            "item_revisions",
            "schedule_settings",
            "sources",
        }
    finally:
        database.dispose()


def test_content_source_migration_disables_qwen_without_deleting_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "qwen-migration.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "f2c7a93d1b44")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(id, name, source_type, start_url, enabled, collector_name, "
                    "collector_config, requires_custom_collector, origin, created_at, updated_at) "
                    "VALUES (1, 'Qwen-Agent Releases', 'github_release', "
                    "'https://github.com/QwenLM/Qwen-Agent/releases', 1, "
                    "'github_release', '{}', 0, 'preset', :now, :now)"
                ),
                {"now": "2026-07-19 00:00:00"},
            )
            connection.execute(
                text(
                    "INSERT INTO intelligence_items "
                    "(source_id, title, original_url, canonical_url, discovered_at, "
                    "last_seen_at, updated_at, category, fingerprint, is_favorite, "
                    "is_active, extra) "
                    "VALUES (1, '历史版本', 'https://github.com/QwenLM/Qwen-Agent/releases/1', "
                    "'https://github.com/QwenLM/Qwen-Agent/releases/1', :now, :now, :now, "
                    "'agent_product', :fingerprint, 1, 1, '{}')"
                ),
                {"now": "2026-07-19 00:00:00", "fingerprint": "a" * 64},
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with migrated.engine.connect() as connection:
            source_row = connection.execute(
                text(
                    "SELECT enabled, source_kind, homepage_visible, export_visible "
                    "FROM sources WHERE id = 1"
                )
            ).one()
            history = connection.execute(
                text(
                    "SELECT title, is_favorite, manual_category "
                    "FROM intelligence_items WHERE source_id = 1"
                )
            ).one()
        assert tuple(source_row) == (0, "fallback", 0, 0)
        assert tuple(history) == ("历史版本", 1, None)
    finally:
        migrated.dispose()


def test_stage_seven_database_upgrade_and_formal_seed_preserve_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stage-seven-upgrade.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "f2c7a93d1b44")
    database = Database(database_url)
    try:
        now = "2026-07-18 00:00:00"
        with database.engine.begin() as connection:
            for source_id, name, source_type, url, enabled, collector, collector_config in (
                (
                    1,
                    "Google Blog RSS",
                    "rss",
                    "https://blog.google/rss",
                    1,
                    "rss",
                    '{"max_items": 100}',
                ),
                (
                    2,
                    "AIIA",
                    "html_list",
                    "https://www.aiiaorg.cn/",
                    0,
                    "html_list",
                    '{"allowed_domains":["www.aiiaorg.cn","mp.weixin.qq.com"],'
                    '"discovery":{"mode":"selectors","max_pages":1,"max_depth":0,'
                    '"max_items":100},"extraction":{"item_selector":'
                    '".news-scroll-area div.cursor-pointer","title_selector":"h3",'
                    '"date_selector":"span","embedded_title_key":"title",'
                    '"embedded_link_key":"external_url"}}',
                ),
                (
                    3,
                    "Qwen-Agent Releases",
                    "github_release",
                    "https://github.com/QwenLM/Qwen-Agent/releases",
                    1,
                    "github_release",
                    '{"max_releases":100,"include_prereleases":false}',
                ),
            ):
                connection.execute(
                    text(
                        "INSERT INTO sources (id,name,source_type,start_url,enabled,"
                        "collector_name,collector_config,default_category,"
                        "requires_custom_collector,origin,"
                        "created_at,updated_at) VALUES (:id,:name,:type,:url,:enabled,:collector,"
                        ":collector_config,:default_category,0,'preset',:now,:now)"
                    ),
                    {
                        "id": source_id,
                        "name": name,
                        "type": source_type,
                        "url": url,
                        "enabled": enabled,
                        "collector": collector,
                        "collector_config": collector_config,
                        "default_category": (
                            "policy_industry"
                            if name == "AIIA"
                            else "agent_product"
                            if name == "Qwen-Agent Releases"
                            else None
                        ),
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO intelligence_items (source_id,title,original_url,canonical_url,"
                    "discovered_at,last_seen_at,updated_at,category,manual_category,fingerprint,"
                    "is_favorite,is_active,extra) VALUES (3,'历史版本','https://example.com/old',"
                    "'https://example.com/old',:now,:now,:now,'agent_product','award_case',"
                    ":fingerprint,1,1,'{}')"
                ),
                {"now": now, "fingerprint": "f" * 64},
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    upgraded = Database(database_url)
    try:
        with RepositoryUnitOfWork(upgraded) as uow:
            sources = uow.sources.list()
            historical_item = uow.items.list()[0]
        assert len(sources) == 3
        assert [source.enabled for source in sources] == [True, False, False]
        assert [source.source_kind.value for source in sources] == ["test", "test", "fallback"]
        for source in sources:
            ContentAdmissionPolicy().validate_source(source)
            assert isinstance(source.content_scope, list)
            assert isinstance(source.include_terms, list)
            assert isinstance(source.exclude_terms, list)
            assert source.audience.value == "all"
            assert source.source_tier.value == "fallback"
            assert source.homepage_visible is False
            assert source.export_visible is False
            assert source.minimum_quality_score == 50.0
            assert source.accept_title_only is True
            assert source.allow_technical_updates is False
        qwen = sources[2]
        assert qwen.allow_external_links is False
        assert historical_item.is_favorite is True
        assert historical_item.manual_category is Category.AWARD_CASE
        assert historical_item.admission_accepted is False

        seed = SourceSeedService(lambda: RepositoryUnitOfWork(upgraded))
        first = seed.seed()
        second = seed.seed()
        assert (first.created, first.promoted, first.conflicts) == (25, 0, 1)
        assert (second.created, second.promoted, second.existing, second.conflicts) == (0, 0, 25, 1)
        with RepositoryUnitOfWork(upgraded) as uow:
            final_sources = uow.sources.list()
            aiia = uow.sources.get_by_start_url("https://www.aiiaorg.cn/")
            qwen = uow.sources.get(3)
        assert len(final_sources) == 28
        assert sum(source.source_kind is SourceKind.FORMAL for source in final_sources) == 25
        assert aiia is not None and aiia.allow_external_links is False
        assert aiia.enabled is False
        assert qwen is not None
        assert (
            qwen.enabled,
            qwen.source_kind.value,
            qwen.homepage_visible,
            qwen.export_visible,
        ) == (False, "fallback", False, False)
    finally:
        upgraded.dispose()


def test_web_startup_migration_check_does_not_upgrade_database(tmp_path: Path) -> None:
    database_path = tmp_path / "behind-head.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "8df43a9b1c2e")
    database = Database(database_url)
    try:
        with pytest.raises(RuntimeError, match="数据库结构未升级"):
            require_current_migration(database)
        with database.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "8df43a9b1c2e"
        assert "updated_at" not in {
            column["name"] for column in inspect(database.engine).get_columns("intelligence_items")
        }
    finally:
        database.dispose()


def test_status_migration_preserves_existing_crawl_runs(tmp_path: Path) -> None:
    database_path = tmp_path / "status-migration.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "db0caa03a995")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, status, source_total, source_success, source_failed, "
                    "discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES "
                    "('2026-07-17 00:00:00', 'succeeded', 1, 1, 0, 1, 1, 0, 0, 0), "
                    "('2026-07-17 01:00:00', 'partial', 2, 1, 1, 1, 1, 0, 0, 0), "
                    "('2026-07-17 02:00:00', 'running', 3, 0, 0, 0, 0, 0, 0, 0), "
                    "('2026-07-17 03:00:00', 'failed', 4, 0, 4, 4, 0, 0, 4, 4)"
                )
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with migrated.engine.connect() as connection:
            statuses = list(connection.scalars(text("SELECT status FROM crawl_runs ORDER BY id")))
        assert statuses == ["success", "partial_success", "running", "failed"]
    finally:
        migrated.dispose()

    command.downgrade(config, "db0caa03a995")
    downgraded = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with downgraded.engine.connect() as connection:
            statuses = list(connection.scalars(text("SELECT status FROM crawl_runs ORDER BY id")))
            source_totals = list(
                connection.scalars(text("SELECT source_total FROM crawl_runs ORDER BY id"))
            )
        assert statuses == ["succeeded", "partial", "running", "failed"]
        assert source_totals == [1, 2, 3, 4]
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    repeated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with repeated.engine.connect() as connection:
            statuses = list(connection.scalars(text("SELECT status FROM crawl_runs ORDER BY id")))
        assert statuses == ["success", "partial_success", "running", "failed"]
    finally:
        repeated.dispose()


def test_item_updated_at_migration_preserves_existing_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "item-updated-at-migration.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "8df43a9b1c2e")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(name, source_type, start_url, enabled, collector_name, collector_config, "
                    "requires_custom_collector, origin, created_at, updated_at) VALUES "
                    "('Feed', 'rss', 'https://example.com/feed', 1, 'rss', '{}', 0, "
                    "'preset', '2026-07-18 00:00:00', '2026-07-18 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO intelligence_items "
                    "(source_id, title, original_url, canonical_url, discovered_at, last_seen_at, "
                    "category, fingerprint, is_favorite, is_active, extra) VALUES "
                    "(1, 'Existing item', 'https://example.com/item', 'https://example.com/item', "
                    "'2026-07-18 01:00:00', '2026-07-18 02:00:00', 'unclassified', "
                    f"'{('a' * 64)}', 0, 1, '{{}}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO item_revisions "
                    "(item_id, changed_at, old_data, new_data) VALUES "
                    "(1, '2026-07-18 02:00:00', '{\"title\": \"Old\"}', "
                    '\'{"title": "Existing item"}\')'
                )
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with migrated.engine.connect() as connection:
            row = connection.execute(
                text("SELECT title, last_seen_at, updated_at FROM intelligence_items")
            ).one()
            revision = connection.execute(
                text("SELECT item_id, old_data, new_data FROM item_revisions")
            ).one()
            foreign_key_violations = list(connection.execute(text("PRAGMA foreign_key_check")))
        assert row.title == "Existing item"
        assert row.updated_at == row.last_seen_at
        assert revision.item_id == 1
        assert '"Old"' in revision.old_data
        assert foreign_key_violations == []
    finally:
        migrated.dispose()

    command.downgrade(config, "8df43a9b1c2e")
    downgraded = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        inspector = inspect(downgraded.engine)
        assert "updated_at" not in {
            column["name"] for column in inspector.get_columns("intelligence_items")
        }
        with downgraded.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM intelligence_items")) == 1
            assert connection.scalar(text("SELECT count(*) FROM item_revisions")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    repeated = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with repeated.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM intelligence_items")) == 1
            assert connection.scalar(text("SELECT count(*) FROM item_revisions")) == 1
            assert connection.scalar(text("SELECT updated_at FROM intelligence_items")) == (
                "2026-07-18 02:00:00"
            )
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        repeated.dispose()


def test_source_description_migration_round_trip_preserves_nonempty_graph(tmp_path: Path) -> None:
    database_path = tmp_path / "source-description-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "a51f8e8d29c4")
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(name, source_type, start_url, enabled, collector_name, collector_config, "
                    "requires_custom_collector, origin, created_at, updated_at) VALUES "
                    "('Existing source', 'rss', 'https://example.com/feed', 1, 'rss', '{}', 0, "
                    "'user_added', '2026-07-18 00:00:00', '2026-07-18 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, status, source_total, source_success, source_failed, "
                    "discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES "
                    "('2026-07-18 00:00:00', 'success', 1, 1, 0, 1, 1, 0, 0, 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO intelligence_items "
                    "(source_id, title, original_url, canonical_url, discovered_at, last_seen_at, "
                    "updated_at, category, fingerprint, is_favorite, is_active, extra) VALUES "
                    "(1, 'Existing item', 'https://example.com/item', "
                    "'https://example.com/item', '2026-07-18 01:00:00', "
                    "'2026-07-18 02:00:00', '2026-07-18 02:00:00', 'unclassified', "
                    f"'{('d' * 64)}', 1, 1, '{{}}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO item_revisions "
                    "(item_id, crawl_run_id, changed_at, old_data, new_data) VALUES "
                    "(1, 1, '2026-07-18 02:00:00', '{\"title\": \"Old\"}', "
                    '\'{"title": "Existing item"}\')'
                )
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    migrated = Database(database_url)
    try:
        with migrated.engine.begin() as connection:
            assert connection.scalar(text("SELECT description FROM sources WHERE id = 1")) is None
            connection.execute(
                text(
                    "UPDATE sources SET description = 'review note', name = 'Renamed' WHERE id = 1"
                )
            )
    finally:
        migrated.dispose()

    command.downgrade(config, "a51f8e8d29c4")
    downgraded = Database(database_url)
    try:
        assert "description" not in {
            column["name"] for column in inspect(downgraded.engine).get_columns("sources")
        }
        with downgraded.engine.connect() as connection:
            assert connection.scalar(text("SELECT name FROM sources WHERE id = 1")) == "Renamed"
            assert (
                connection.scalar(text("SELECT origin FROM sources WHERE id = 1")) == "user_added"
            )
            assert connection.scalar(text("SELECT count(*) FROM intelligence_items")) == 1
            assert connection.scalar(text("SELECT count(*) FROM item_revisions")) == 1
            assert connection.scalar(text("SELECT count(*) FROM crawl_runs")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    repeated = Database(database_url)
    try:
        with repeated.engine.connect() as connection:
            assert connection.scalar(text("SELECT description FROM sources WHERE id = 1")) is None
            assert connection.scalar(text("SELECT name FROM sources WHERE id = 1")) == "Renamed"
            assert connection.scalar(text("SELECT is_favorite FROM intelligence_items")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        repeated.dispose()


def test_runtime_scheduling_migration_round_trip_preserves_nonempty_graph(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime-scheduling-migration.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "c94d2a1f7e3b")
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(name, description, source_type, start_url, enabled, collector_name, "
                    "collector_config, requires_custom_collector, origin, created_at, updated_at) "
                    "VALUES ('Existing source', 'note', 'rss', 'https://example.com/feed', 1, "
                    "'rss', '{}', 0, 'user_added', '2026-07-19 00:00:00', "
                    "'2026-07-19 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, finished_at, status, source_total, source_success, "
                    "source_failed, discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES ('2026-07-19 00:00:00', "
                    "'2026-07-19 00:01:00', 'success', 1, 1, 0, 1, 1, 0, 0, 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO intelligence_items "
                    "(source_id, title, original_url, canonical_url, discovered_at, last_seen_at, "
                    "updated_at, category, fingerprint, is_favorite, is_active, extra) VALUES "
                    "(1, 'Existing item', 'https://example.com/item', "
                    "'https://example.com/item', '2026-07-19 00:00:00', "
                    "'2026-07-19 00:01:00', '2026-07-19 00:01:00', 'unclassified', "
                    f"'{('e' * 64)}', 1, 1, '{{}}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO item_revisions "
                    "(item_id, crawl_run_id, changed_at, old_data, new_data) VALUES "
                    "(1, 1, '2026-07-19 00:01:00', '{\"title\": \"Old\"}', "
                    '\'{"title": "Existing item"}\')'
                )
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    migrated = Database(database_url)
    try:
        with migrated.engine.connect() as connection:
            assert connection.scalar(text("SELECT trigger FROM crawl_runs")) == "legacy_manual"
            assert connection.scalar(text("SELECT count(*) FROM schedule_settings")) == 0
            assert connection.scalar(text("SELECT count(*) FROM sources")) == 1
            assert connection.scalar(text("SELECT count(*) FROM intelligence_items")) == 1
            assert connection.scalar(text("SELECT count(*) FROM item_revisions")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        migrated.dispose()

    command.downgrade(config, "c94d2a1f7e3b")
    downgraded = Database(database_url)
    try:
        inspector = inspect(downgraded.engine)
        assert "schedule_settings" not in inspector.get_table_names()
        assert "trigger" not in {column["name"] for column in inspector.get_columns("crawl_runs")}
        with downgraded.engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM sources")) == 1
            assert connection.scalar(text("SELECT count(*) FROM intelligence_items")) == 1
            assert connection.scalar(text("SELECT count(*) FROM item_revisions")) == 1
            assert connection.scalar(text("SELECT count(*) FROM crawl_runs")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        downgraded.dispose()

    command.upgrade(config, "head")
    repeated = Database(database_url)
    try:
        with repeated.engine.connect() as connection:
            assert connection.scalar(text("SELECT trigger FROM crawl_runs")) == "legacy_manual"
            assert connection.scalar(text("SELECT title FROM intelligence_items")) == (
                "Existing item"
            )
            assert connection.scalar(text("SELECT is_favorite FROM intelligence_items")) == 1
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        repeated.dispose()


def test_runtime_scheduling_migration_rejects_unknown_status_before_schema_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime-scheduling-unknown-status.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "c94d2a1f7e3b")
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, status, source_total, source_success, source_failed, "
                    "discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES "
                    "('2026-07-19 00:00:00', 'mystery', 1, 0, 1, 0, 0, 0, 0, 0)"
                )
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    finally:
        database.dispose()

    with pytest.raises(RuntimeError, match=r"unknown status.*mystery"):
        command.upgrade(config, "head")

    preserved = Database(database_url)
    try:
        inspector = inspect(preserved.engine)
        with preserved.engine.connect() as connection:
            assert connection.scalar(text("SELECT status FROM crawl_runs")) == "mystery"
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "c94d2a1f7e3b"
            )
        assert "trigger" not in {column["name"] for column in inspector.get_columns("crawl_runs")}
        assert "schedule_settings" not in inspector.get_table_names()
    finally:
        preserved.dispose()


def test_status_migration_rejects_unknown_history_before_schema_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "unknown-status-migration.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "db0caa03a995")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                text(
                    "INSERT INTO crawl_runs "
                    "(started_at, status, source_total, source_success, source_failed, "
                    "discovered_count, new_count, updated_count, skipped_count, "
                    "unclassified_count) VALUES "
                    "('2026-07-17 00:00:00', 'mystery', 1, 0, 1, 0, 0, 0, 0, 0)"
                )
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    finally:
        database.dispose()

    with pytest.raises(RuntimeError, match=r"unknown status.*mystery"):
        command.upgrade(config, "head")

    preserved = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with preserved.engine.connect() as connection:
            status = connection.scalar(text("SELECT status FROM crawl_runs"))
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert status == "mystery"
        assert revision == "db0caa03a995"
    finally:
        preserved.dispose()


def test_canonical_url_is_unique(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        repositories.sources.add(source)
        repositories.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="First title",
                original_url="https://example.com/articles/1?utm_source=test",
                canonical_url="https://example.com/articles/1",
                category=Category.UNCLASSIFIED,
                fingerprint="a" * 64,
            )
        )

    try:
        with RepositoryUnitOfWork(database) as repositories:
            repositories.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    title="Duplicate URL",
                    original_url="https://example.com/articles/1",
                    canonical_url="https://example.com/articles/1",
                    category=Category.UNCLASSIFIED,
                    fingerprint="b" * 64,
                )
            )
    except IntegrityError:
        pass
    else:
        raise AssertionError("Duplicate canonical URL should violate the database constraint")


def test_source_fingerprint_pair_is_unique(database: Database, source: Source) -> None:
    with RepositoryUnitOfWork(database) as repositories:
        repositories.sources.add(source)
        repositories.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="First title",
                original_url="https://example.com/one",
                canonical_url="https://example.com/one",
                fingerprint="same-fingerprint",
            )
        )

    try:
        with RepositoryUnitOfWork(database) as repositories:
            repositories.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    title="Same content at another URL",
                    original_url="https://example.com/two",
                    canonical_url="https://example.com/two",
                    fingerprint="same-fingerprint",
                )
            )
    except IntegrityError:
        pass
    else:
        raise AssertionError("Duplicate source/fingerprint should violate the database constraint")
