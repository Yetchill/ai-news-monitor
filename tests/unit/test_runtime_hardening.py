"""Focused regression tests for desktop runtime and AI hardening."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

import app.classifiers.providers as provider_module
from app.classifiers.providers import (
    BatchClassificationItem,
    BatchClassificationResult,
    LLMResponseError,
    OpenAICompatibleProvider,
    parse_batch_response,
)
from app.config.paths import APP_DIRECTORY_NAME, default_data_dir
from app.config.settings import Settings
from app.domain.enums import Category
from app.domain.models import AIProviderSetting, AISettings
from app.fetchers.errors import ResponseTooLargeFetchError
from app.fetchers.http import HttpFetcher
from app.services.ai_operation_service import (
    AIOperationService,
    ClassificationRequestStats,
    _needs_smart_review,  # pyright: ignore[reportPrivateUsage]
)
from app.services.background_tasks import BackgroundTaskManager
from app.services.release_sanitization import create_sanitized_database_copy
from app.services.single_instance import (
    SingleInstanceError,
    SingleInstanceLock,
    _ensure_lock_byte,  # pyright: ignore[reportPrivateUsage]
)
from app.storage.database import Database
from app.storage.migrations.runtime import current_and_head, ensure_database_current


@pytest.mark.asyncio
async def test_provider_reuses_injected_client_for_multiple_requests() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        content = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"category":"irrelevant","confidence":0.9,"reason":"弱关联"}'
                            )
                        }
                    }
                ]
            }
        )
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            "https://example.com", "test-key", "test-model", client=client
        )
        first = await provider.classify("招聘信息提到 AI", None, "来源", None)
        second = await provider.classify("普通财经新闻", None, "来源", None)
        await provider.aclose()

        assert requests == 2
        assert first.category is second.category is Category.IRRELEVANT
        assert not client.is_closed


@pytest.mark.asyncio
async def test_provider_owned_client_is_created_once_and_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[FakeClient] = []

    class FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False
            self.posts = 0
            created.append(self)

        async def post(self, url: str, *, json: object) -> httpx.Response:
            del json
            self.posts += 1
            body = {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"category":"agent_product","confidence":0.9,"reason":"产品发布"}'
                            )
                        }
                    }
                ]
            }
            return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", FakeClient)
    provider = OpenAICompatibleProvider("https://example.com", "test-key", "test-model")

    await provider.classify("智能体产品发布", None, "来源", None)
    await provider.classify("AI 助手上线", None, "来源", None)
    await provider.aclose()

    assert len(created) == 1
    assert created[0].posts == 2
    assert created[0].closed


def test_quick_batch_accepts_id_and_category_only() -> None:
    parsed = parse_batch_response(
        '[{"id":"7","category":"irrelevant"}]',
        {"7"},
        require_confidence=False,
    )

    assert parsed.failures == {}
    assert parsed.results["7"].category is Category.IRRELEVANT
    assert parsed.results["7"].confidence == 0


def test_smart_gate_uses_context_and_boundary_signals() -> None:
    ranking = BatchClassificationItem("1", "某模型登顶 benchmark 排行榜", "该内容只是技术评测榜单")
    clear = BatchClassificationItem("2", "某公司正式发布智能体助手产品", "产品今日正式开放使用")

    assert _needs_smart_review(
        BatchClassificationResult("1", Category.AWARD_CASE, 0.98, ""), ranking
    )
    assert not _needs_smart_review(
        BatchClassificationResult("2", Category.AGENT_PRODUCT, 0.95, ""), clear
    )


@pytest.mark.asyncio
async def test_batch_fallback_has_depth_and_request_caps(database: Database) -> None:
    class AlwaysInvalid:
        async def classify_batch(
            self,
            items: list[BatchClassificationItem],
            *,
            include_summaries: bool,
        ):  # type: ignore[no-untyped-def]
            del items, include_summaries
            raise LLMResponseError("bad json")

    service = AIOperationService(database)
    stats = ClassificationRequestStats(max_requests=50)
    items = [BatchClassificationItem(str(index), f"标题 {index}") for index in range(15)]
    result = await service._classify_batch_resilient(  # pyright: ignore[reportPrivateUsage]
        AlwaysInvalid(),  # type: ignore[arg-type]
        items,
        include_summaries=False,
        max_retries=0,
        output_mode="quick",
        stats=stats,
        depth=0,
    )

    assert set(result.failures) == {item.id for item in items}
    assert stats.model_requests <= stats.max_requests
    assert stats.parse_failures == stats.model_requests
    assert stats.splits > 0


@pytest.mark.asyncio
async def test_background_manager_cancels_and_drains_tasks() -> None:
    manager = BackgroundTaskManager()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = manager.start(worker(), name="managed-test-task")
    await started.wait()
    await manager.aclose()

    assert task.cancelled()
    assert cancelled.is_set()
    assert manager.active_count == 0


def test_sqlite_enables_wal_busy_timeout_and_foreign_keys(database: Database) -> None:
    with database.engine.connect() as connection:
        journal = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert str(journal).casefold() == "wal"
    assert timeout == 10_000
    assert foreign_keys == 1


@pytest.mark.asyncio
async def test_http_fetcher_stops_when_stream_exceeds_limit() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2049)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = HttpFetcher(
            client=client,
            request_interval_seconds=0,
            max_response_bytes=2048,
        )
        with pytest.raises(ResponseTooLargeFetchError):
            await fetcher.fetch("https://example.com/large")


def test_windows_data_path_and_settings_are_coherent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("AIM_DATA_DIR", raising=False)
    monkeypatch.delenv("AIM_DATABASE_URL", raising=False)
    monkeypatch.delenv("AIM_LOG_DIR", raising=False)
    monkeypatch.delenv("AIM_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    expected = tmp_path / APP_DIRECTORY_NAME
    settings = Settings(data_dir=expected, _env_file=None)  # pyright: ignore[reportCallIssue]

    assert default_data_dir() == expected
    assert settings.database_url == f"sqlite:///{(expected / 'intelligence.db').as_posix()}"
    assert settings.log_dir == expected / "logs"
    assert settings.output_dir == expected / "output"


def test_runtime_migration_initializes_empty_database(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}")
    try:
        ensure_database_current(database)
        assert current_and_head(database)[0] == current_and_head(database)[1]
    finally:
        database.dispose()


def test_release_copy_removes_keys_without_touching_live_database(
    database: Database, tmp_path: Path
) -> None:
    with database.session() as session:
        session.add(
            AIProviderSetting(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                model="deepseek-chat",
                api_key="provider-secret",
            )
        )
        session.add(AISettings(id=1, api_key="legacy-secret"))

    destination = create_sanitized_database_copy(database.database_url, tmp_path / "release.db")
    with sqlite3.connect(destination) as connection:
        assert connection.execute(
            "SELECT api_key FROM ai_provider_settings WHERE provider='deepseek'"
        ).fetchone() == ("",)
        assert connection.execute("SELECT api_key FROM ai_settings WHERE id=1").fetchone() == ("",)
    with database.session() as session:
        assert session.get(AIProviderSetting, "deepseek").api_key == "provider-secret"  # type: ignore[union-attr]
        assert session.get(AISettings, 1).api_key == "legacy-secret"  # type: ignore[union-attr]


def test_single_instance_lock_rejects_second_owner(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'single.db').as_posix()}"
    first = SingleInstanceLock(url)
    second = SingleInstanceLock(url)
    first.acquire()
    try:
        with pytest.raises(SingleInstanceError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_windows_lock_byte_initialization_does_not_grow_existing_file() -> None:
    empty = io.BytesIO()
    _ensure_lock_byte(empty)
    _ensure_lock_byte(empty)
    assert empty.getvalue() == b"0"
    assert empty.tell() == 0

    existing = io.BytesIO(b"already-initialized")
    _ensure_lock_byte(existing)
    assert existing.getvalue() == b"already-initialized"
    assert existing.tell() == 0
