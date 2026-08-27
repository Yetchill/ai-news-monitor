# pyright: reportUnknownArgumentType=false, reportUnknownLambdaType=false
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Stage-2 multi-provider, connection, batch classification, and job tests."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.classifiers.providers import (
    BatchClassificationItem,
    BatchClassificationResult,
    BatchParseResult,
    ConnectionTestResult,
    LLMAuthenticationError,
    LLMModelError,
    LLMNetworkError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleProvider,
    parse_batch_response,
)
from app.domain.enums import Category, SourceOrigin, SourceType
from app.domain.models import AIJob, AISettings, IntelligenceItem, Source
from app.services.ai_operation_service import AIOperationService
from app.services.ai_settings_service import AIConfig, AISettingsService
from app.storage.database import Database
from app.web.app import create_app


@pytest.fixture
def stage2_app(database: Database) -> FastAPI:
    return create_app(database=database, enforce_migrations=False)


@pytest.fixture
def stage2_client(stage2_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(stage2_app, raise_server_exceptions=False) as client:
        yield client


def _config(provider: str, key: str, *, model: str | None = None) -> AIConfig:
    defaults = {
        "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
        "openai": ("https://api.openai.com", "gpt-test"),
        "openrouter": ("https://openrouter.ai/api", "router-test"),
        "custom": ("https://custom.example/v1", "custom-test"),
    }
    base_url, default_model = defaults[provider]
    selected_model = model or default_model
    return AIConfig(
        provider=provider,
        base_url=base_url,
        model=selected_model,
        api_key=key,
        timeout_seconds=20,
        max_retries=2,
        enabled=True,
        classifier_mode="manual",
        summarizer_mode="manual",
        classification_provider=provider,
        classification_model=selected_model,
        summarization_provider=provider,
        summarization_model=selected_model,
        classification_batch_mode="smart",
    )


def _seed_items(database: Database, count: int) -> list[int]:
    with database.session() as session:
        source = Source(
            name="Stage 2",
            source_type=SourceType.RSS,
            start_url="https://stage2.example/feed",
            collector_name="rss",
            collector_config={},
            origin=SourceOrigin.USER_ADDED,
        )
        session.add(source)
        session.flush()
        ids: list[int] = []
        for index in range(count):
            item = IntelligenceItem(
                source_id=source.id,
                title=f"资讯标题 {index}",
                summary=f"数据库已有摘要 {index}",
                original_url=f"https://stage2.example/{index}",
                canonical_url=f"https://stage2.example/{index}",
                fingerprint=f"{index + 1000:064x}",
                category=Category.UNCLASSIFIED,
                is_active=True,
                admission_accepted=True,
            )
            session.add(item)
            session.flush()
            ids.append(item.id)
        session.commit()
        return ids


class RecordingBatchProvider:
    def __init__(self, *, low_every: int = 0) -> None:
        self.low_every = low_every
        self.calls: list[tuple[bool, list[BatchClassificationItem]]] = []

    async def classify_batch(
        self,
        items: list[BatchClassificationItem],
        *,
        include_summaries: bool,
    ) -> BatchParseResult:
        self.calls.append((include_summaries, items))
        results: dict[str, BatchClassificationResult] = {}
        for item in reversed(items):
            low = (
                not include_summaries and self.low_every > 0 and int(item.id) % self.low_every == 0
            )
            results[item.id] = BatchClassificationResult(
                id=item.id,
                category=Category.AGENT_PRODUCT,
                confidence=0.5 if low else 0.95,
                reason="标题边界模糊" if low else "明确产品发布",
            )
        return BatchParseResult(results, {})


def test_provider_keys_are_independent_replaceable_and_clearable(database: Database) -> None:
    service = AISettingsService(database)
    service.save(_config("deepseek", "deepseek-secret"))
    service.save(_config("openai", "openai-secret"))

    assert service.get_config("deepseek").api_key == "deepseek-secret"
    assert service.get_config("openai").api_key == "openai-secret"
    assert service.get_config("openrouter").api_key == ""

    service.save(_config("deepseek", "deepseek-replacement"))
    service.clear_key("deepseek")

    assert service.get_config("deepseek").api_key == ""
    assert service.get_config("openai").api_key == "openai-secret"


def test_empty_key_preserves_only_selected_provider(database: Database) -> None:
    service = AISettingsService(database)
    service.save(_config("deepseek", "deepseek-secret"))
    service.save(_config("openai", "openai-secret"))
    service.save(_config("openai", "", model="gpt-new"))

    assert service.get_config("openai").api_key == "openai-secret"
    assert service.get_config("openai").model == "gpt-new"
    assert service.get_config("deepseek").api_key == "deepseek-secret"


def test_classification_and_summary_can_select_different_models(database: Database) -> None:
    service = AISettingsService(database)
    service.save(_config("deepseek", "deepseek-secret"))
    service.save(_config("openai", "openai-secret"))
    workflow = _config("deepseek", "")
    workflow.classification_provider = "deepseek"
    workflow.classification_model = "deepseek-classifier"
    workflow.summarization_provider = "openai"
    workflow.summarization_model = "openai-summarizer"
    service.save(workflow)

    classification = service.get_operation_config("classification")
    summarization = service.get_operation_config("summarization")

    assert (classification.provider, classification.model, classification.api_key) == (
        "deepseek",
        "deepseek-classifier",
        "deepseek-secret",
    )
    assert (summarization.provider, summarization.model, summarization.api_key) == (
        "openai",
        "openai-summarizer",
        "openai-secret",
    )


def test_legacy_singleton_key_migrates_to_original_provider(database: Database) -> None:
    with database.session() as session:
        session.add(
            AISettings(
                id=1,
                provider="openai",
                base_url="https://legacy.example",
                model="legacy-model",
                api_key="legacy-secret",
            )
        )
        session.commit()

    config = AISettingsService(database).get_config("openai")

    assert config.api_key == "legacy-secret"
    with database.session() as session:
        legacy = session.get(AISettings, 1)
        assert legacy is not None
        assert legacy.api_key == ""


def test_provider_api_and_html_never_return_plain_key(
    database: Database, stage2_client: TestClient
) -> None:
    secret = "plain-key-must-not-leak"
    AISettingsService(database).save(_config("deepseek", secret))

    api = stage2_client.get("/ai/providers/deepseek")
    page = stage2_client.get("/ai")

    assert secret not in api.text
    assert "api_key" not in api.json()
    assert secret not in page.text


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (LLMAuthenticationError("bad"), "API Key 无效"),
        (LLMTimeoutError("slow"), "请求超时"),
        (LLMModelError("missing"), "模型不存在或无权限"),
        (LLMNetworkError("offline"), "网络连接失败"),
    ],
)
def test_connection_failures_are_mapped_to_sanitized_chinese(
    monkeypatch: pytest.MonkeyPatch,
    stage2_client: TestClient,
    error: Exception,
    message: str,
) -> None:
    async def fail(_provider: OpenAICompatibleProvider) -> ConnectionTestResult:
        raise error

    monkeypatch.setattr(OpenAICompatibleProvider, "test_connection", fail)
    response = stage2_client.post(
        "/ai/test-connection",
        headers={"Accept": "application/json"},
        data={
            "provider": "deepseek",
            "base_url": "https://mock.example",
            "model": "mock-model",
            "api_key": "test-secret",
            "timeout_seconds": "5",
        },
    )

    assert response.status_code == 200
    assert response.json()["message"] == message
    assert "test-secret" not in response.text


def test_connection_success_returns_provider_model_and_latency(
    monkeypatch: pytest.MonkeyPatch, stage2_client: TestClient
) -> None:
    async def succeed(_provider: OpenAICompatibleProvider) -> ConnectionTestResult:
        return ConnectionTestResult("OK", 1640)

    monkeypatch.setattr(OpenAICompatibleProvider, "test_connection", succeed)
    response = stage2_client.post(
        "/ai/test-connection",
        headers={"Accept": "application/json"},
        data={
            "provider": "deepseek",
            "base_url": "https://mock.example",
            "model": "deepseek-chat",
            "api_key": "new-unsaved-key",
            "timeout_seconds": "5",
        },
    )
    body = response.json()

    assert body["ok"] is True
    assert body["provider_label"] == "DeepSeek"
    assert body["model"] == "deepseek-chat"
    assert body["latency_ms"] == 1640
    assert "new-unsaved-key" not in response.text


def test_connection_error_cannot_echo_api_key(
    monkeypatch: pytest.MonkeyPatch, stage2_client: TestClient
) -> None:
    secret = "provider-echoed-secret"

    async def fail(_provider: OpenAICompatibleProvider) -> ConnectionTestResult:
        raise LLMProviderError(f"upstream echoed {secret}")

    monkeypatch.setattr(OpenAICompatibleProvider, "test_connection", fail)
    response = stage2_client.post(
        "/ai/test-connection",
        headers={"Accept": "application/json"},
        data={
            "provider": "custom",
            "base_url": "https://mock.example",
            "model": "mock-model",
            "api_key": secret,
            "timeout_seconds": "5",
        },
    )

    assert secret not in response.text
    assert "[REDACTED]" in response.text


def test_connection_button_has_immediate_and_terminal_states(stage2_client: TestClient) -> None:
    script = stage2_client.get("/static/app.js").text

    assert 'testBtn.textContent = "正在测试连接…"' in script
    assert 'testBtn.setAttribute("aria-busy", "true")' in script
    assert "testBtn.disabled = false" in script
    assert 'testBtn.textContent = "测试连接"' in script


def test_batch_parser_matches_out_of_order_and_rejects_bad_ids() -> None:
    parsed = parse_batch_response(
        json.dumps(
            [
                {
                    "id": "2",
                    "category": "agent_product",
                    "confidence": 0.9,
                    "reason": "two",
                },
                {
                    "id": "1",
                    "category": "model_technology",
                    "confidence": 0.8,
                    "reason": "one",
                },
                {
                    "id": "unknown",
                    "category": "award_case",
                    "confidence": 0.9,
                    "reason": "bad",
                },
            ]
        ),
        {"1", "2"},
    )

    assert parsed.results["1"].category is Category.MODEL_TECHNOLOGY
    assert parsed.results["2"].category is Category.AGENT_PRODUCT
    assert parsed.unknown_ids == ("unknown",)


def test_batch_parser_detects_missing_duplicate_and_illegal_category() -> None:
    parsed = parse_batch_response(
        json.dumps(
            [
                {"id": "1", "category": "agent_product", "confidence": 0.9, "reason": "a"},
                {"id": "1", "category": "agent_product", "confidence": 0.9, "reason": "b"},
                {"id": "2", "category": "not-real", "confidence": 0.9, "reason": "c"},
            ]
        ),
        {"1", "2", "3"},
    )

    assert parsed.results == {}
    assert parsed.failures == {
        "1": "模型返回重复 ID",
        "2": "模型返回非法分类",
        "3": "模型结果漏项",
    }


@pytest.mark.parametrize(
    ("mode", "expected_summary_flags"),
    [
        ("quick", [False]),
        ("precise", [True]),
    ],
)
async def test_quick_and_precise_modes_use_expected_database_fields(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    mode: str,
    expected_summary_flags: list[bool],
) -> None:
    ids = _seed_items(database, 5)
    AISettingsService(database).save(_config("deepseek", "fake-key"))
    fake = RecordingBatchProvider()
    monkeypatch.setattr("app.services.ai_operation_service._build_provider", lambda _config: fake)

    job = await AIOperationService(database).classify_batch(ids, mode=mode)

    assert job.status == "completed"
    assert [flag for flag, _items in fake.calls] == expected_summary_flags


async def test_smart_mode_batches_100_items_and_reviews_only_low_confidence(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    ids = _seed_items(database, 100)
    AISettingsService(database).save(_config("deepseek", "fake-key"))
    fake = RecordingBatchProvider(low_every=6)
    monkeypatch.setattr("app.services.ai_operation_service._build_provider", lambda _config: fake)

    job = await AIOperationService(database).classify_batch(ids, mode="smart")
    title_calls = [items for include, items in fake.calls if not include]
    summary_calls = [items for include, items in fake.calls if include]

    assert job.status == "completed"
    assert len(title_calls) == 7
    assert len(summary_calls) == 2
    assert len(fake.calls) == 9
    assert job.total_batches == 9
    assert job.current_batch == 9
    assert job.processed_count == 100
    assert job.success_count == 100
    assert job.failure_count == 0
    assert all(len(batch) <= 15 for batch in title_calls + summary_calls)
    assert sum(len(batch) for batch in summary_calls) == len(
        [item_id for item_id in ids if item_id % 6 == 0]
    )


async def test_invalid_large_batch_is_split_without_losing_other_items(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    ids = _seed_items(database, 10)
    AISettingsService(database).save(_config("deepseek", "fake-key"))

    class SplitProvider(RecordingBatchProvider):
        async def classify_batch(
            self,
            items: list[BatchClassificationItem],
            *,
            include_summaries: bool,
        ) -> BatchParseResult:
            self.calls.append((include_summaries, items))
            if len(items) > 4:
                raise LLMResponseError("invalid batch JSON")
            return await super().classify_batch(items, include_summaries=include_summaries)

    fake = SplitProvider()
    monkeypatch.setattr("app.services.ai_operation_service._build_provider", lambda _config: fake)

    job = await AIOperationService(database).classify_batch(ids, mode="quick")

    assert job.status == "completed"
    assert job.success_count == 10
    assert max(len(items) for _include, items in fake.calls[1:]) <= 5


async def test_rate_limit_uses_bounded_retry(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    ids = _seed_items(database, 3)
    config = _config("deepseek", "fake-key")
    config.max_retries = 1
    AISettingsService(database).save(config)

    class RateLimitOnceProvider(RecordingBatchProvider):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def classify_batch(
            self,
            items: list[BatchClassificationItem],
            *,
            include_summaries: bool,
        ) -> BatchParseResult:
            self.attempts += 1
            if self.attempts == 1:
                raise LLMRateLimitError("rate limited")
            return await super().classify_batch(items, include_summaries=include_summaries)

    fake = RateLimitOnceProvider()
    monkeypatch.setattr("app.services.ai_operation_service._build_provider", lambda _config: fake)

    job = await AIOperationService(database).classify_batch(ids, mode="quick")

    assert job.status == "completed"
    assert fake.attempts == 2


async def test_manual_category_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    ids = _seed_items(database, 2)
    with database.session() as session:
        item = session.get(IntelligenceItem, ids[0])
        assert item is not None
        item.manual_category = Category.AWARD_CASE
        session.commit()
    AISettingsService(database).save(_config("deepseek", "fake-key"))
    fake = RecordingBatchProvider()
    monkeypatch.setattr("app.services.ai_operation_service._build_provider", lambda _config: fake)

    await AIOperationService(database).classify_batch(ids, mode="quick", reclassify=True)

    with database.session() as session:
        protected = session.get(IntelligenceItem, ids[0])
        assert protected is not None
        assert protected.manual_category is Category.AWARD_CASE
        assert protected.category is Category.UNCLASSIFIED


def test_duplicate_active_task_is_reused(database: Database) -> None:
    ids = _seed_items(database, 3)
    AISettingsService(database).save(_config("deepseek", "fake-key"))
    service = AIOperationService(database)

    first, first_created = service.enqueue_classification(
        ids, trigger="manual", mode="smart", reclassify=False
    )
    second, second_created = service.enqueue_classification(
        ids, trigger="manual", mode="smart", reclassify=False
    )

    assert first_created is True
    assert second_created is False
    assert first.job_id == second.job_id


def test_interrupted_jobs_are_failed_on_recovery(database: Database) -> None:
    with database.session() as session:
        session.add(
            AIJob(
                job_type="classification",
                trigger="manual",
                status="running",
                total_count=10,
                provider="deepseek",
                model="fake",
                started_at=datetime.now(UTC),
            )
        )
        session.commit()

    count = AIOperationService(database).recover_interrupted_jobs()

    assert count == 1
    with database.session() as session:
        job = session.query(AIJob).one()
        assert job.status == "failed"
        assert job.error_summary is not None
        assert "中断" in job.error_summary
