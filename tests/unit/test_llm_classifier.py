"""Mock-based tests for LLM and Hybrid classifiers."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.classifiers.hybrid import HybridClassifier
from app.classifiers.llm import LLMClassifier
from app.classifiers.providers import (
    LLMConfigError,
    LLMProviderError,
    LLMResponse,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleProvider,
    build_prompt,
    parse_response,
)
from app.classifiers.rule_based import RuleBasedClassifier
from app.domain.collection import CollectedItem
from app.domain.enums import Category


def _mock_json(mock_response: Any, data: Any) -> None:
    mock_response.json.return_value = {
        "choices": [{"message": {"content": json.dumps(data)}}]
    }


def _item(title: str, summary: str | None = None) -> CollectedItem:
    return CollectedItem(
        title=title,
        summary=summary,
        original_url="https://example.com/item",
        canonical_url="https://example.com/item",
    )


class FakeProvider:
    """Mock provider that returns a predefined response."""

    def __init__(
        self,
        response: LLMResponse | None = None,
        error: type[Exception] | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str | None, str, str | None]] = []

    async def classify(
        self,
        title: str,
        summary: str | None,
        source_name: str,
        source_role: str | None,
    ) -> LLMResponse:
        self.calls.append((title, summary, source_name, source_role))
        if self.error is not None:
            raise self.error("simulated")
        if self.response is None:
            raise LLMProviderError("no response configured")
        return self.response


# --- LLMClassifier mock tests ---

async def test_llm_classifier_returns_valid_result() -> None:
    provider = FakeProvider(
        LLMResponse(Category.AGENT_PRODUCT, 0.92, "智能体平台上线")
    )
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("智能体产品正式发布"))

    assert result.category is Category.AGENT_PRODUCT
    assert abs(result.score - 9.2) < 0.01
    assert result.provider == "llm"
    assert "0.92" in result.reason


async def test_llm_classifier_fallback_on_timeout() -> None:
    provider = FakeProvider(error=LLMTimeoutError)
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("任意标题"))

    assert result.category is Category.UNCLASSIFIED
    assert result.provider == "llm"
    assert "超时" in result.reason


async def test_llm_classifier_fallback_on_invalid_json() -> None:
    provider = FakeProvider(error=LLMResponseError)
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("任意标题"))

    assert result.category is Category.UNCLASSIFIED
    assert "无效" in result.reason


async def test_llm_classifier_fallback_on_network_error() -> None:
    provider = FakeProvider(error=LLMProviderError)
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("任意标题"))

    assert result.category is Category.UNCLASSIFIED
    assert "失败" in result.reason


async def test_llm_classifier_marks_low_confidence_as_ambiguous() -> None:
    provider = FakeProvider(
        LLMResponse(Category.POLICY_INDUSTRY, 0.45, "不太确定")
    )
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("政策相关文章"))

    assert result.is_ambiguous is True


async def test_llm_classifier_high_confidence_not_ambiguous() -> None:
    provider = FakeProvider(
        LLMResponse(Category.MODEL_TECHNOLOGY, 0.95, "明确的模型发布")
    )
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("大模型发布"))

    assert result.is_ambiguous is False


# --- OpenAICompatibleProvider mock tests ---

_BASE = "https://test.example.com"


async def test_openai_provider_parses_valid_json() -> None:
    response_data = {"category": "model_technology", "confidence": 0.88, "reason": "b"}

    mock_client = AsyncMock()
    mock_response = MagicMock()
    _mock_json(mock_response, response_data)
    mock_client.post.return_value = mock_response

    provider = OpenAICompatibleProvider(_BASE, "sk-test", "test-model")
    provider._client = lambda: mock_client  # type: ignore[method-assign]

    result = await provider.classify("大模型开源", None, "测试来源", "official_product")

    assert result.category is Category.MODEL_TECHNOLOGY
    assert result.confidence == 0.88
    assert mock_client.post.called


async def test_openai_provider_rejects_invalid_category() -> None:
    response_data = {"category": "non_existent", "confidence": 0.8, "reason": "t"}
    mock_client = AsyncMock()
    mock_response = MagicMock()
    _mock_json(mock_response, response_data)
    mock_client.post.return_value = mock_response

    provider = OpenAICompatibleProvider(_BASE, "sk-test", "test-model")
    provider._client = lambda: mock_client  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="未知分类"):
        await provider.classify("test", None, "test", None)


async def test_openai_provider_rejects_confidence_oob() -> None:
    response_data = {"category": "model_technology", "confidence": 1.5, "reason": "t"}
    mock_client = AsyncMock()
    mock_response = MagicMock()
    _mock_json(mock_response, response_data)
    mock_client.post.return_value = mock_response

    provider = OpenAICompatibleProvider(_BASE, "sk-test", "test-model")
    provider._client = lambda: mock_client  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="越界"):
        await provider.classify("test", None, "test", None)


async def test_openai_provider_rejects_invalid_json() -> None:
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "not json"}}]
    }
    mock_client.post.return_value = mock_response

    provider = OpenAICompatibleProvider(_BASE, "sk-test", "test-model")
    provider._client = lambda: mock_client  # type: ignore[method-assign]

    with pytest.raises(LLMResponseError, match="非法 JSON"):
        await provider.classify("test", None, "test", None)


async def test_openai_provider_raises_without_api_key() -> None:
    with pytest.raises(LLMConfigError, match="API_KEY"):
        OpenAICompatibleProvider(_BASE, "", "test-model")


# --- HybridClassifier mock tests ---

async def test_hybrid_skips_llm_when_rule_high_confidence() -> None:
    rule = RuleBasedClassifier.from_yaml()
    provider = FakeProvider(LLMResponse(Category.MODEL_TECHNOLOGY, 0.9, "test"))
    hybrid = HybridClassifier(rule, provider)

    result = await hybrid.classify(_item("大模型正式发布开源"))

    assert result.provider != "hybrid" or "规则" in result.reason
    assert len(provider.calls) == 0


async def test_hybrid_calls_llm_when_rule_unclassified() -> None:
    rule = RuleBasedClassifier.from_yaml()
    provider = FakeProvider(
        LLMResponse(Category.AGENT_PRODUCT, 0.85, "智能体上线")
    )
    hybrid = HybridClassifier(rule, provider)

    result = await hybrid.classify(_item("春季校园招聘正式开始"))

    assert len(provider.calls) == 1
    assert result.category is Category.AGENT_PRODUCT
    assert result.provider == "hybrid"


async def test_hybrid_falls_back_to_rule_when_llm_fails() -> None:
    rule = RuleBasedClassifier.from_yaml()
    provider = FakeProvider(error=LLMProviderError)
    hybrid = HybridClassifier(rule, provider)

    result = await hybrid.classify(_item("春季校园招聘正式开始"))

    assert result.provider == "hybrid"
    assert result.category is Category.UNCLASSIFIED


async def test_hybrid_rejects_low_confidence_llm() -> None:
    rule = RuleBasedClassifier.from_yaml()
    provider = FakeProvider(LLMResponse(Category.AWARD_CASE, 0.3, "low"))
    hybrid = HybridClassifier(rule, provider)

    result = await hybrid.classify(_item("春季校园招聘正式开始"))

    assert len(provider.calls) == 1
    assert "置信度不足" in result.reason


async def test_hybrid_uses_rule_when_llm_returns_unclassified() -> None:
    rule = RuleBasedClassifier.from_yaml()
    provider = FakeProvider(LLMResponse(Category.UNCLASSIFIED, 0.8, "unclear"))
    hybrid = HybridClassifier(rule, provider)

    result = await hybrid.classify(_item("春季校园招聘正式开始"))

    assert result.category is Category.UNCLASSIFIED
    assert result.provider == "hybrid"


# --- Prompt and parsing tests ---

def testbuild_prompt_includes_all_fields() -> None:
    prompt = build_prompt("测试标题", "测试摘要", "测试来源", "official_product")

    assert "测试标题" in prompt
    assert "测试摘要" in prompt
    assert "测试来源" in prompt
    assert "official_product" in prompt
    assert "category" in prompt
    assert "confidence" in prompt


def testbuild_prompt_without_optional_fields() -> None:
    prompt = build_prompt("测试标题", None, "来源", None)

    assert "测试标题" in prompt
    assert "来源" in prompt
    assert "model_technology" in prompt


def testparse_response_valid() -> None:
    response = parse_response(
        json.dumps(
            {"category": "agent_product", "confidence": 0.92, "reason": "智能体上线"}
        ),
        0.7,
    )

    assert response.category is Category.AGENT_PRODUCT
    assert response.confidence == 0.92
    assert response.reason == "智能体上线"


def testparse_response_truncates_long_reason() -> None:
    long_reason = "x" * 300
    response = parse_response(
        json.dumps(
            {"category": "model_technology", "confidence": 0.8, "reason": long_reason}
        ),
        0.7,
    )

    assert len(response.reason) <= 200


# --- Default mode test (rule classifier works without API key) ---

async def test_rule_classifier_works_without_api_key() -> None:
    """Default mode 'rule' should work without any API key configured."""
    classifier = RuleBasedClassifier.from_yaml()

    result = await classifier.classify(_item("大模型发布"))

    assert result.category is Category.MODEL_TECHNOLOGY
    assert result.provider == "rule_based"


# --- API key not leaked in logs ---

async def test_error_does_not_contain_api_key() -> None:
    provider = FakeProvider(error=LLMProviderError)
    classifier = LLMClassifier(provider)

    result = await classifier.classify(_item("test"))

    assert "sk-" not in result.reason
    assert "api_key" not in result.reason.lower()
    assert "key" not in result.reason.lower()
