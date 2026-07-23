"""LLM-based classification using a configured model provider."""

import logging

from app.classifiers.providers import (
    LLMConfigError,
    LLMProvider,
    LLMProviderError,
    LLMResponseError,
    LLMTimeoutError,
)
from app.domain.classification import ClassificationResult
from app.domain.collection import CollectedItem
from app.domain.enums import Category

logger = logging.getLogger(__name__)


class LLMClassifier:
    """Classify articles by calling a configured LLM provider."""

    provider = "llm"

    def __init__(
        self,
        provider: LLMProvider,
        *,
        source_name: str = "",
        source_role: str | None = None,
    ) -> None:
        self._provider = provider
        self._source_name = source_name
        self._source_role = source_role

    async def classify(
        self,
        item: CollectedItem,
        *,
        source_default: Category | str | None = None,
    ) -> ClassificationResult:
        try:
            llm_result = await self._provider.classify(
                title=item.title,
                summary=item.summary,
                source_name=self._source_name,
                source_role=self._source_role,
            )
        except LLMConfigError:
            raise
        except LLMTimeoutError:
            logger.warning("LLM 分类超时, 回退到 unclassified")
            return ClassificationResult(
                category=Category.UNCLASSIFIED,
                score=0.0,
                reason="LLM 请求超时, 自动回退为 unclassified。",
                provider=self.provider,
            )
        except LLMResponseError as exc:
            logger.warning(f"LLM 返回无效响应: {exc}")
            return ClassificationResult(
                category=Category.UNCLASSIFIED,
                score=0.0,
                reason=f"LLM 返回无效响应 ({exc}), 自动回退为 unclassified。",
                provider=self.provider,
            )
        except LLMProviderError as exc:
            logger.warning(f"LLM 调用失败: {exc}")
            return ClassificationResult(
                category=Category.UNCLASSIFIED,
                score=0.0,
                reason=f"LLM 调用失败 ({exc}), 自动回退为 unclassified。",
                provider=self.provider,
            )

        return ClassificationResult(
            category=llm_result.category,
            score=llm_result.confidence * 10,
            reason=(
                f"LLM 分类 {llm_result.category.value}: 置信度 {llm_result.confidence:.2f}. "
                f"{llm_result.reason}"
            ),
            provider=self.provider,
            is_ambiguous=llm_result.confidence < 0.6,
        )


def create_llm_classifier(
    *,
    source_name: str = "",
    source_role: str | None = None,
) -> LLMClassifier:
    from app.classifiers.providers import DeepSeekProvider

    return LLMClassifier(
        DeepSeekProvider(),
        source_name=source_name,
        source_role=source_role,
    )
