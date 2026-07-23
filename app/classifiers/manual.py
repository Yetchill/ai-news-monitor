"""Manual classification parsing and final category precedence."""

from app.domain.classification import ClassificationResult
from app.domain.enums import Category


class ManualCategoryError(ValueError):
    """Raised when a persisted/user supplied manual category is invalid."""


class ManualClassifier:
    """Convert an optional manual category value into an explainable override."""

    provider = "manual"

    def classify(self, manual_category: Category | str | None) -> ClassificationResult | None:
        if manual_category is None or manual_category == "":
            return None
        try:
            category = Category(manual_category)
        except ValueError as exc:
            raise ManualCategoryError(f"未知人工分类: {manual_category}") from exc
        return ClassificationResult(
            category=category,
            score=1.0,
            reason=f"人工分类 {category.value} 拥有最高优先级, 覆盖自动分类结果。",
            provider=self.provider,
            matched_rules=(f"manual_category:{category.value}",),
        )


class FinalCategoryResolver:
    """Apply the stable manual-over-automatic precedence without mutating an item."""

    def __init__(self, manual_classifier: ManualClassifier | None = None) -> None:
        self._manual_classifier = manual_classifier or ManualClassifier()

    def resolve(
        self,
        automatic_result: ClassificationResult,
        *,
        manual_category: Category | str | None = None,
    ) -> ClassificationResult:
        return self._manual_classifier.classify(manual_category) or automatic_result
