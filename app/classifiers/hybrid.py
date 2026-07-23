"""Hybrid classifier combining rule-based scoring and LLM fallback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.classifiers.providers import (
    LLMConfigError,
    LLMProvider,
    LLMProviderError,
    LLMResponseError,
)
from app.domain.classification import ClassificationResult
from app.domain.collection import CollectedItem
from app.domain.enums import Category

if TYPE_CHECKING:
    from app.classifiers.rule_based import RuleBasedClassifier

logger = logging.getLogger(__name__)


class HybridClassifier:
    """Rule-first with LLM fallback for unclassified or low-confidence rule results."""

    provider = "hybrid"

    def __init__(
        self,
        rule_classifier: RuleBasedClassifier,
        llm_provider: LLMProvider,
        *,
        source_name: str = "",
        source_role: str | None = None,
        confidence_threshold: float = 0.7,
    ) -> None:
        from app.classifiers.llm import LLMClassifier

        self._rule = rule_classifier
        self._llm = LLMClassifier(
            llm_provider,
            source_name=source_name,
            source_role=source_role,
        )
        self._confidence_threshold = confidence_threshold

    async def classify(
        self,
        item: CollectedItem,
        *,
        source_default: Category | str | None = None,
    ) -> ClassificationResult:
        rule_result = await self._rule.classify(item, source_default=source_default)

        if rule_result.provider == "manual":
            return rule_result

        if _should_skip_llm(rule_result):
            return rule_result

        try:
            llm_result = await self._llm.classify(item, source_default=source_default)
        except LLMConfigError:
            return rule_result
        except (LLMProviderError, LLMResponseError):
            logger.warning("Hybrid: LLM 调用失败, 回退到规则结果")
            return rule_result

        if _is_llm_result_usable(llm_result, self._confidence_threshold):
            llm_conf = llm_result.score / 10
            return ClassificationResult(
                category=llm_result.category,
                score=llm_result.score,
                reason=(
                    f"Hybrid: 规则输出 {rule_result.category.value} "
                    f"(得分 {rule_result.score:.2f}), "
                    f"LLM 覆盖为 {llm_result.category.value} "
                    f"(置信度 {llm_conf:.2f}). "
                    f"{llm_result.reason}"
                ),
                provider=self.provider,
            )

        return ClassificationResult(
            category=rule_result.category,
            score=rule_result.score,
            reason=(
                f"Hybrid: LLM 置信度不足, 保留规则结果 "
                f"{rule_result.category.value}. "
                f"{rule_result.reason}"
            ),
            provider=self.provider,
        )


def _should_skip_llm(result: ClassificationResult) -> bool:
    """Return True when rule result is confident enough to skip LLM call."""

    if result.is_ambiguous:
        return False
    if result.category is Category.UNCLASSIFIED:
        return False
    return True


def _is_llm_result_usable(result: ClassificationResult, threshold: float) -> bool:
    if result.category is Category.UNCLASSIFIED:
        return False
    confidence = result.score / 10.0
    return confidence >= threshold
