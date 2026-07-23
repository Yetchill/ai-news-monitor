# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
"""Canonical source-catalog reconciliation and Web projection tests."""

import hashlib
import re
from pathlib import Path

import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config.settings import PROJECT_ROOT
from app.domain.enums import Category, LifecycleState, SourceOrigin, SourceType
from app.domain.models import IntelligenceItem, Source
from app.services.source_catalog_service import (
    CATALOG_PATH,
    SourceCatalogService,
    load_source_catalog,
)
from app.services.source_seed_service import DEFAULT_PRESET_PATH
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app

ACTIVE_COUNT = 18
CANDIDATE_COUNT = 8
CATALOG_COUNT = 26
RETIRED_NAMES = {
    "Google Blog RSS",
    "OpenAI News RSS",
    "Qwen-Agent Releases",
    "百度智能云客户案例",
    "国家数据局政策发布",
    "国家网信办网信发布",
}


def _legacy_source(
    slug: str,
    name: str,
    url: str,
    *,
    enabled: bool = True,
) -> Source:
    return Source(
        slug=slug,
        name=name,
        source_type=SourceType.RSS,
        start_url=url,
        enabled=enabled,
        lifecycle_state=LifecycleState.ACTIVE if enabled else LifecycleState.PAUSED,
        collector_name="rss",
        collector_config={},
        origin=SourceOrigin.PRESET,
    )


def _catalog_sets() -> tuple[set[str], set[str]]:
    entries = load_source_catalog()
    return (
        {entry.slug for entry in entries if entry.lifecycle_state is LifecycleState.ACTIVE},
        {entry.slug for entry in entries if entry.lifecycle_state is LifecycleState.CANDIDATE},
    )


def _page_slugs(html: str) -> set[str]:
    return set(re.findall(r'data-slug="([a-z0-9-]+)"', html))


def test_canonical_catalog_is_the_unique_expected_yaml() -> None:
    candidates = [
        path.resolve()
        for path in PROJECT_ROOT.rglob("*.yaml")
        if "source" in path.name.lower() and "catalog" in path.name.lower()
    ]
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    entries = load_source_catalog()
    active, candidate = _catalog_sets()

    assert CATALOG_PATH.resolve() == (PROJECT_ROOT / "app/config/source_catalog.yaml").resolve()
    assert DEFAULT_PRESET_PATH.resolve() == CATALOG_PATH.resolve()
    assert candidates == [CATALOG_PATH.resolve()]
    assert raw["version"] == 1
    assert len(entries) == CATALOG_COUNT
    assert len(active) == ACTIVE_COUNT
    assert len(candidate) == CANDIDATE_COUNT
    assert {"nda-news", "cac-policy-regulations"} <= active
    assert not RETIRED_NAMES & {entry.name for entry in entries}
    assert hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest() == (
        "bb4612292b25ffadae0d3bbd971d1a868229f09e18db46861761a8ab89c7940f"
    )


def test_migration_preserves_an_old_seed_until_explicit_reconcile(tmp_path: Path) -> None:
    database_path = tmp_path / "old-seed-upgrade.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "f2c7a93d1b44")
    database = Database(database_url)
    try:
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO sources "
                    "(id,name,source_type,start_url,enabled,collector_name,collector_config,"
                    "requires_custom_collector,origin,created_at,updated_at) VALUES "
                    "(1,'Google Blog RSS','rss','https://blog.google/rss/',1,'rss','{}',0,"
                    "'preset',:now,:now)"
                ),
                {"now": "2026-07-18 00:00:00"},
            )
    finally:
        database.dispose()

    command.upgrade(config, "head")
    upgraded = Database(database_url)
    try:
        with RepositoryUnitOfWork(upgraded) as uow:
            old = uow.sources.list()
        assert [(source.name, source.slug, source.catalog_managed) for source in old] == [
            ("Google Blog RSS", "legacy-source-1", False)
        ]

        result = SourceCatalogService(lambda: RepositoryUnitOfWork(upgraded)).sync(
            reconcile=True
        )
        with RepositoryUnitOfWork(upgraded) as uow:
            current = uow.sources.list()
        assert (result.created, result.deleted, result.retired, result.conflicts) == (26, 1, 0, 0)
        assert {source.slug for source in current} == {
            entry.slug for entry in load_source_catalog()
        }
    finally:
        upgraded.dispose()


def test_fresh_head_migration_does_not_implicitly_seed_sources(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh-head.db"
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
    database = Database(f"sqlite:///{database_path.as_posix()}")
    try:
        with RepositoryUnitOfWork(database) as uow:
            assert uow.sources.list() == []
    finally:
        database.dispose()


def test_reconcile_replaces_legacy_names_removes_unreferenced_rows_and_is_idempotent(
    database: Database,
) -> None:
    legacy_rows = (
        ("legacy-source-1", "国家数据局政策发布", "https://www.nda.gov.cn/sjj/zwgk/zcfb/list/index_pc_1.html"),
        ("legacy-source-2", "国家网信办网信发布", "https://www.cac.gov.cn/wxzw/wxfb/A093702index_1.htm"),
        ("legacy-source-3", "AIIA 人工智能产业发展联盟", "https://www.aiiaorg.cn/"),
        ("legacy-source-4", "Google Blog RSS", "https://blog.google/rss/"),
        ("legacy-source-5", "OpenAI News RSS", "https://openai.com/news/rss.xml"),
        ("legacy-source-6", "Qwen-Agent Releases", "https://github.com/QwenLM/Qwen-Agent/releases"),
        ("legacy-source-7", "百度智能云客户案例", "https://cloud.baidu.com/case/index.html"),
    )
    with RepositoryUnitOfWork(database) as uow:
        for slug, name, url in legacy_rows:
            uow.sources.add(_legacy_source(slug, name, url))

    service = SourceCatalogService(lambda: RepositoryUnitOfWork(database))
    first = service.sync(reconcile=True)
    second = service.sync(reconcile=True)
    active, candidate = _catalog_sets()
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()

    assert (first.updated, first.deleted, first.retired, first.conflicts) == (3, 4, 0, 0)
    assert (second.created, second.updated, second.deleted, second.retired) == (0, 0, 0, 0)
    assert second.existing == CATALOG_COUNT
    assert {source.slug for source in sources} == active | candidate
    assert {source.slug for source in sources if source.enabled} == active
    assert {
        source.slug for source in sources if source.lifecycle_state is LifecycleState.CANDIDATE
    } == candidate
    assert all(not source.enabled for source in sources if source.slug in candidate)
    assert not RETIRED_NAMES & {source.name for source in sources}
    assert {"国家数据局新闻动态", "国家网信办政策法规"} <= {
        source.name for source in sources
    }


def test_reconcile_retires_but_does_not_delete_a_legacy_source_with_items(
    database: Database,
) -> None:
    with RepositoryUnitOfWork(database) as uow:
        source = uow.sources.add(
            _legacy_source("legacy-source-99", "Google Blog RSS", "https://blog.google/rss/")
        )
        item = uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="历史资讯",
                original_url="https://example.com/history",
                canonical_url="https://example.com/history",
                category=Category.UNCLASSIFIED,
                fingerprint="a" * 64,
            )
        )

    result = SourceCatalogService(lambda: RepositoryUnitOfWork(database)).sync(reconcile=True)
    with RepositoryUnitOfWork(database) as uow:
        retained = uow.sources.get(source.id)
        retained_item = uow.items.get(item.id)

    assert (result.deleted, result.retired) == (0, 1)
    assert retained is not None and retained_item is not None
    assert retained_item.source_id == retained.id
    assert retained.enabled is False
    assert retained.lifecycle_state is LifecycleState.PAUSED
    assert retained.catalog_managed is False


def test_sources_get_is_read_only_and_tabs_match_catalog(database: Database) -> None:
    service = SourceCatalogService(lambda: RepositoryUnitOfWork(database))
    service.sync(reconcile=True)
    active, candidate = _catalog_sets()
    with RepositoryUnitOfWork(database) as uow:
        before = [(source.id, source.slug, source.updated_at) for source in uow.sources.list()]

    with TestClient(create_app(database=database, enforce_migrations=False)) as client:
        monitoring = client.get("/sources", params={"per_page": 100})
        candidates = client.get("/sources", params={"tab": "candidate", "per_page": 100})
        homepage = client.get("/", params={"per_page": 100})

    with RepositoryUnitOfWork(database) as uow:
        after = [(source.id, source.slug, source.updated_at) for source in uow.sources.list()]

    assert monitoring.status_code == candidates.status_code == homepage.status_code == 200
    assert _page_slugs(monitoring.text) == active
    assert _page_slugs(candidates.text) == candidate
    assert "监控中 <span class=\"tab-count\">18</span>" in monitoring.text
    assert "候选 <span class=\"tab-count\">8</span>" in candidates.text
    for entry in load_source_catalog():
        if entry.slug in active:
            assert entry.name in homepage.text
        else:
            assert entry.name not in homepage.text
    assert not RETIRED_NAMES & set(homepage.text.splitlines())
    assert before == after
