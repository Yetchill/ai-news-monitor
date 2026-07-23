"""Persistence-independent classification domain values."""

from dataclasses import dataclass
from typing import Protocol

from app.domain.collection import CollectedItem
from app.domain.enums import Category


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Explainable outcome returned by every classifier implementation."""

    category: Category
    score: float
    reason: str
    provider: str
    matched_rules: tuple[str, ...] = ()
    is_ambiguous: bool = False


class Classifier(Protocol):
    """Classify a collected item without persistence or collector coupling."""

    async def classify(
        self,
        item: CollectedItem,
        *,
        source_default: Category | str | None = None,
    ) -> ClassificationResult: ...
