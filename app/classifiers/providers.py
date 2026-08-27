"""Model-agnostic LLM provider abstraction with OpenAI-compatible (DeepSeek) support."""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx

from app.config.settings import get_settings
from app.domain.enums import Category

logger = logging.getLogger(__name__)

_TAXONOMY_DEFINITION = """分类体系与边界:
- model_technology: 基础模型、算法、训练/推理框架或研究突破; Agent 框架仅在面向开发者的技术框架时归此类
- agent_product: 可供用户使用的智能体、AI 助手、Copilot、Agent 平台/SDK 或产品功能发布
- enterprise_case: 企业已在具体业务中采用 AI 并有投产、流程或成效; 企业自有产品发布不算应用案例
- award_case: 正式奖项、案例评选、入围/获奖名单、公示或表彰; benchmark/leaderboard 第一名不算正式获奖
- solicitation: 征集、申报、参评、招募、报名开放或截止提醒
- policy_industry: 政策、监管、标准、白皮书、行业报告及产业统计
- irrelevant: 内容本身不是值得关注的 AI 情报(仅顺带提 AI、招聘营销、泛科技/财经等)
- unclassified: 确属 AI 情报, 但以上类别仍无法可靠归类
冲突优先级: 征集动作→solicitation; 正式评选结果→award_case; 已落地采用→enterprise_case; 用户产品→agent_product; 底层技术→model_technology。"""  # noqa: E501


class LLMProviderError(Exception):
    """Base error for LLM provider failures."""


class LLMTimeoutError(LLMProviderError):
    """Request timed out."""


class LLMResponseError(LLMProviderError):
    """Invalid or unparseable response from the model."""


class LLMConfigError(LLMProviderError):
    """Configuration error (missing key, wrong URL, etc)."""


class LLMAuthenticationError(LLMProviderError):
    """Credential is invalid or rejected."""


class LLMModelError(LLMProviderError):
    """Requested model does not exist or is not available."""


class LLMRateLimitError(LLMProviderError):
    """Provider rate limit was reached."""


class LLMNetworkError(LLMProviderError):
    """Provider could not be reached."""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Structured output from a single LLM classification call."""

    category: Category
    confidence: float
    reason: str
    raw_text: str = ""


@dataclass(frozen=True, slots=True)
class BatchClassificationItem:
    """One article included in a batch classification request."""

    id: str
    title: str
    summary: str | None = None
    source_name: str = ""
    source_role: str = ""


@dataclass(frozen=True, slots=True)
class BatchClassificationResult:
    """One validated, ID-addressed result returned by a batch request."""

    id: str
    category: Category
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class BatchParseResult:
    results: dict[str, BatchClassificationResult]
    failures: dict[str, str]
    unknown_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    content: str
    latency_ms: int


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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise LLMConfigError("AIM_LLM_API_KEY 未设置, 无法初始化 LLM Provider")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._confidence_threshold = confidence_threshold
        self._http_client = client
        self._owns_client = client is None

    async def classify(
        self,
        title: str,
        summary: str | None,
        source_name: str,
        source_role: str | None,
    ) -> LLMResponse:
        prompt = build_prompt(title, summary, source_name, source_role)
        raw = await self._chat(
            [
                {
                    "role": "system",
                    "content": "你是一个 AI 情报分类助手。只输出严格的 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        content = extract_content(raw)
        return parse_response(content, self._confidence_threshold)

    async def classify_batch(
        self,
        items: list[BatchClassificationItem],
        *,
        include_summaries: bool,
        output_mode: str = "smart",
    ) -> BatchParseResult:
        """Classify a real batch in one model request and match only by stable ID."""

        if not items:
            return BatchParseResult({}, {})
        raw = await self._chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 AI 情报批量分类助手。只能输出一个 JSON 数组,"
                        "不得遗漏或添加 ID, 也不得依赖输入顺序。"
                    ),
                },
                {
                    "role": "user",
                    "content": build_batch_prompt(
                        items,
                        include_summaries=include_summaries,
                        output_mode=output_mode,
                    ),
                },
            ],
            max_tokens=_batch_max_tokens(len(items), output_mode),
        )
        return parse_batch_response(
            extract_content(raw),
            {item.id for item in items},
            require_confidence=output_mode != "quick",
        )

    async def test_connection(self) -> ConnectionTestResult:
        """Send a minimal completion and measure the actual round-trip with a monotonic clock."""

        started = time.monotonic()
        # Preserve a narrow seam for existing mock providers while production
        # always uses the minimal completion below.
        if type(self).classify is not _ORIGINAL_CLASSIFY_METHOD:
            mocked = await self.classify("连接测试", None, "", None)
            return ConnectionTestResult(
                content=mocked.reason or "OK",
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
        raw = await self._chat(
            [
                {
                    "role": "user",
                    "content": "只返回 OK, 不要输出任何其他内容。",
                }
            ],
            max_tokens=8,
        )
        content = extract_content(raw).strip()
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        if not content:
            raise LLMResponseError("模型返回内容为空")
        return ConnectionTestResult(content=content, latency_ms=latency_ms)

    async def complete(self, prompt: str, *, max_tokens: int = 300) -> str:
        """Return plain completion text for summarization."""

        raw = await self._chat(
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return extract_content(raw)

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float = 0,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        client = self._client()
        try:
            response = await client.post(self._chat_completions_url(), json=payload)
            response.raise_for_status()
            body: object = response.json()
            if not isinstance(body, dict):
                raise LLMResponseError("模型返回格式异常")
            return cast(dict[str, Any], body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("请求超时") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise LLMAuthenticationError("API Key 无效") from exc
            if status == 404:
                raise LLMModelError("模型不存在或无权限") from exc
            if status == 429:
                raise LLMRateLimitError("请求被限流") from exc
            if status >= 500:
                raise LLMProviderError("供应商服务暂时不可用") from exc
            raise LLMProviderError(f"模型请求失败 (HTTP {status})") from exc
        except (httpx.RequestError, httpx.NetworkError) as exc:
            raise LLMNetworkError("网络连接失败") from exc
        except (ValueError, TypeError) as exc:
            raise LLMResponseError("返回格式异常") from exc

    def _chat_completions_url(self) -> str:
        if self._base_url.endswith("/v1"):
            return f"{self._base_url}/chat/completions"
        return f"{self._base_url}/v1/chat/completions"

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client

    async def aclose(self) -> None:
        """Close the shared pool once the provider/job lifecycle ends."""

        client = self._http_client
        if self._owns_client and client is not None:
            self._http_client = None
            await client.aclose()


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
        '\n输出严格 JSON: {"category": "类别值", "confidence": 0.0-1.0, '
        '"reason": "分类理由(50字以内)"}\n'
        "confidence 表示你对分类的把握程度。理由应具体,引用标题或摘要中的关键信息。"
    )
    return "\n".join(parts)


def build_batch_prompt(
    items: list[BatchClassificationItem],
    *,
    include_summaries: bool,
    output_mode: str = "smart",
) -> str:
    records: list[dict[str, str]] = []
    for item in items:
        record = {"id": item.id, "title": item.title}
        if item.source_name:
            record["source"] = item.source_name
        if item.source_role:
            record["source_role"] = item.source_role
        if include_summaries:
            record["context"] = (item.summary or "")[:1600]
        records.append(record)
    fields = "ID、标题和已有摘要" if include_summaries else "ID和标题"
    if output_mode == "quick":
        output_rule = '[{"id":"原ID","category":"类别值"}]'
        mode_rule = "快速模式只返回 id 和 category, 不要 confidence 或 reason。"
    elif output_mode == "precise":
        output_rule = '[{"id":"原ID","category":"类别值","confidence":0.0,"reason":"20字以内依据"}]'
        mode_rule = "精确模式可给极短依据, 禁止复述输入。"
    else:
        output_rule = '[{"id":"原ID","category":"类别值","confidence":0.0}]'
        mode_rule = "智能模式只返回置信度, 不要 reason。"
    return (
        f"请只依据每条资讯的{fields}完成分类, 不得访问链接或补充外部信息。\n"
        f"{_TAXONOMY_DEFINITION}\n"
        f"返回严格 JSON 数组, 每个输入恰好一项: {output_rule}。{mode_rule}\n"
        "弱 AI 关联返回 irrelevant; 属于 AI 但类别不明才返回 unclassified。"
        "不要把 benchmark 排名当正式 award_case。\n"
        f"输入:{json.dumps(records, ensure_ascii=False)}"
    )


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


def parse_batch_response(
    content: str,
    expected_ids: set[str],
    *,
    require_confidence: bool = True,
) -> BatchParseResult:
    """Validate batch IDs/categories without ever pairing by array position."""

    try:
        payload_object: object = json.loads(content)
    except json.JSONDecodeError as exc:
        raise LLMResponseError("批量结果不是合法 JSON") from exc
    if isinstance(payload_object, dict):
        payload_object = cast(dict[str, object], payload_object).get("results")
    if not isinstance(payload_object, list):
        raise LLMResponseError("批量结果不是数组")
    payload = cast(list[object], payload_object)

    results: dict[str, BatchClassificationResult] = {}
    failures: dict[str, str] = {}
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        record = cast(dict[str, object], raw)
        raw_id = record.get("id")
        item_id = str(raw_id) if isinstance(raw_id, (str, int)) else ""
        if item_id not in expected_ids:
            if item_id:
                unknown.append(item_id)
            continue
        if item_id in seen:
            results.pop(item_id, None)
            failures[item_id] = "模型返回重复 ID"
            continue
        seen.add(item_id)
        category_value = record.get("category")
        try:
            category = Category(category_value)
        except (TypeError, ValueError):
            failures[item_id] = "模型返回非法分类"
            continue
        confidence_value = record.get("confidence")
        if confidence_value is None and not require_confidence:
            confidence = 0.0
        elif not isinstance(confidence_value, (int, float)):
            failures[item_id] = "模型返回非法置信度"
            continue
        else:
            confidence = float(confidence_value)
        if not 0 <= confidence <= 1:
            failures[item_id] = "模型返回置信度越界"
            continue
        reason_value = record.get("reason", "")
        reason = reason_value if isinstance(reason_value, str) else str(reason_value)
        results[item_id] = BatchClassificationResult(
            id=item_id,
            category=category,
            confidence=confidence,
            reason=reason[:200],
        )
    for missing_id in expected_ids - seen:
        failures[missing_id] = "模型结果漏项"
    return BatchParseResult(results, failures, tuple(unknown))


def _batch_max_tokens(item_count: int, output_mode: str) -> int:
    per_item = {"quick": 28, "smart": 48, "precise": 80}.get(output_mode, 48)
    minimum = {"quick": 128, "smart": 256, "precise": 384}.get(output_mode, 256)
    return max(minimum, item_count * per_item)


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


_ORIGINAL_CLASSIFY_METHOD = OpenAICompatibleProvider.classify
