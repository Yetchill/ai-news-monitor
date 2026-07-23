"""Model-agnostic LLM provider abstraction with OpenAI-compatible (DeepSeek) support."""

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.config.settings import get_settings
from app.domain.enums import Category

logger = logging.getLogger(__name__)

_TAXONOMY_DEFINITION = """分类体系（Category taxonomy）：
- model_technology: 大模型/算法/推理框架的发布、升级、开源、退役、技术突破、研究进展
- agent_product: 智能体平台、Agent 产品、AI 助手、Copilot、SDK、工作流平台的产品发布与功能更新
- enterprise_case: 企业在具体业务场景中应用 AI 的落地案例、投产消息、降本增效实践
- award_case: 案例评选结果公布、获奖名单公示、入围榜单、表彰通知
- solicitation: 案例征集启动、项目申报通知、参评招募、报名开放、截止提醒
- policy_industry: 政策发布、标准制定、白皮书、行业报告、管理办法、征求意见、监管规定"""  # noqa: RUF001


class LLMProviderError(Exception):
    """Base error for LLM provider failures."""


class LLMTimeoutError(LLMProviderError):
    """Request timed out."""


class LLMResponseError(LLMProviderError):
    """Invalid or unparseable response from the model."""


class LLMConfigError(LLMProviderError):
    """Configuration error (missing key, wrong URL, etc)."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Structured output from a single LLM classification call."""

    category: Category
    confidence: float
    reason: str
    raw_text: str = ""


class LLMProvider(Protocol):
    """A model provider that classifies a single article into a taxonomy category."""

    async def classify(
        self,
        title: str,
        summary: str | None,
        source_name: str,
        source_role: str | None,
    ) -> LLMResponse: ...


class OpenAICompatibleProvider:
    """Provider for OpenAI-compatible chat completions APIs (OpenAI, DeepSeek, etc)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30,
        confidence_threshold: float = 0.7,
    ) -> None:
        if not api_key:
            raise LLMConfigError("AIM_LLM_API_KEY 未设置, 无法初始化 LLM Provider")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._confidence_threshold = confidence_threshold

    async def classify(
        self,
        title: str,
        summary: str | None,
        source_name: str,
        source_role: str | None,
    ) -> LLMResponse:
        prompt = build_prompt(title, summary, source_name, source_role)
        client = self._client()

        try:
            response = await client.post(
                f"{self._base_url}/v1/chat/completions",
                json={
                    "model": self._model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你是一个 AI 情报分类助手。"
                                "只输出严格的 JSON, 不输出任何其他内容。"
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0,
                    "max_tokens": 256,
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(f"LLM 请求超时 ({self._timeout}s)") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                raise LLMResponseError("LLM 请求被限流 (429)") from exc
            if status >= 500:
                raise LLMResponseError(f"LLM 服务端错误 ({status})") from exc
            raise LLMProviderError(f"LLM 请求失败 (HTTP {status})") from exc
        except (httpx.RequestError, httpx.NetworkError) as exc:
            raise LLMProviderError(f"LLM 网络错误: {exc}") from exc

        raw = response.json()
        content = extract_content(raw)
        return parse_response(content, self._confidence_threshold)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )


class DeepSeekProvider(OpenAICompatibleProvider):
    """Pre-configured provider for DeepSeek V3 API."""

    def __init__(self) -> None:
        settings = get_settings()
        super().__init__(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=float(settings.llm_timeout_seconds),
            confidence_threshold=settings.llm_confidence_threshold,
        )


def build_prompt(
    title: str,
    summary: str | None,
    source_name: str,
    source_role: str | None,
) -> str:
    parts: list[str] = [
        f"请将以下文章分类到以下类别之一：\n{_TAXONOMY_DEFINITION}\n"  # noqa: RUF001
    ]
    parts.append(f"文章标题: {title}")
    if summary:
        parts.append(f"文章摘要: {summary}")
    parts.append(f"来源名称: {source_name}")
    if source_role:
        parts.append(f"来源角色: {source_role}")

    parts.append(
        "\n输出严格 JSON: {\"category\": \"类别值\", \"confidence\": 0.0-1.0, "
        "\"reason\": \"分类理由(50字以内)\"}\n"
        "confidence 表示你对分类的把握程度。理由应具体,引用标题或摘要中的关键信息。"
    )
    return "\n".join(parts)


def extract_content(response_body: dict[str, Any]) -> str:
    choices: list[dict[str, Any]] = response_body.get("choices", [])
    if not choices:
        raise LLMResponseError("LLM 返回空 choices")
    message: dict[str, Any] | None = choices[0].get("message")
    if message is None:
        raise LLMResponseError("LLM 返回缺少 message")
    content: str | None = message.get("content")
    if not content:
        raise LLMResponseError("LLM 返回空 content")
    return content.strip()


def parse_response(content: str, confidence_threshold: float) -> LLMResponse:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(f"LLM 返回非法 JSON: {exc}") from exc

    raw_category = data.get("category")
    if not isinstance(raw_category, str):
        raise LLMResponseError("LLM 返回缺少 category 字段或类型不正确")

    try:
        category = Category(raw_category)
    except ValueError as exc:
        raise LLMResponseError(f"LLM 返回未知分类: {raw_category}") from exc

    raw_confidence = data.get("confidence")
    if not isinstance(raw_confidence, (int, float)):
        raise LLMResponseError("LLM 返回 confidence 不是数值")
    confidence = float(raw_confidence)
    if not (0.0 <= confidence <= 1.0):
        raise LLMResponseError(f"LLM 返回 confidence {confidence} 越界")

    reason = data.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    if len(reason) > 200:
        reason = reason[:200]

    return LLMResponse(
        category=category,
        confidence=confidence,
        reason=reason,
        raw_text=content,
    )
