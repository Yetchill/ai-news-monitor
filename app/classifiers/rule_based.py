"""Explainable YAML-driven rule classifier."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.classifiers.rules import ClassificationRules, load_classification_rules
from app.config.settings import PROJECT_ROOT
from app.domain.classification import ClassificationResult
from app.domain.collection import CollectedItem
from app.domain.enums import Category
from app.utils.text import normalize_text

DEFAULT_RULE_PATH = PROJECT_ROOT / "app" / "config" / "classification_rules.yaml"
_ASCII_TERM = re.compile(r"^[a-z0-9][a-z0-9+.#/-]*$")


@dataclass(frozen=True, slots=True)
class _RankedCategory:
    category: Category
    score: float
    priority: int
    matches: tuple[str, ...]


class RuleBasedClassifier:
    """Score title, summary, exclusions, and source defaults using YAML rules."""

    provider = "rule_based"

    def __init__(self, rules: ClassificationRules) -> None:
        self.rules = rules

    @classmethod
    def from_yaml(cls, path: Path = DEFAULT_RULE_PATH) -> "RuleBasedClassifier":
        return cls(load_classification_rules(path))

    async def classify(
        self,
        item: CollectedItem,
        *,
        source_default: Category | str | None = None,
    ) -> ClassificationResult:
        title = normalize_text(item.title)
        summary = normalize_text(item.summary)
        default = _parse_source_default(source_default)
        ranked: list[_RankedCategory] = []

        for category, category_rules in self.rules.categories.items():
            score = 0.0
            matches: list[str] = []
            for field_name, text, weight in (
                ("title", title, self.rules.title_weight),
                ("summary", summary, self.rules.summary_weight),
            ):
                positive_matches = _positive_matches(
                    text,
                    category_rules.phrases,
                    category_rules.keywords,
                    weight=weight,
                    phrase_weight=self.rules.phrase_weight,
                )
                negative_matches = _term_matches(
                    text,
                    {**self.rules.global_negative_phrases, **category_rules.negative_phrases},
                    weight=weight,
                    multiplier=1.0,
                    rule_type="negative",
                )
                for rule_type, term, contribution in positive_matches + negative_matches:
                    score += contribution
                    matches.append(
                        f"{category.value}.{field_name}.{rule_type}:{term} ({contribution:+.2f})"
                    )
            ranked.append(_RankedCategory(category, score, category_rules.priority, tuple(matches)))

        ranked.sort(key=lambda candidate: (candidate.score, candidate.priority), reverse=True)
        first, second = ranked[0], ranked[1]
        margin = first.score - second.score

        if first.score < self.rules.minimum_score:
            return self._fallback(default, ranked, first)
        if margin < self.rules.minimum_margin:
            return ClassificationResult(
                category=Category.UNCLASSIFIED,
                score=first.score,
                reason=(
                    f"规则结果待确认: {first.category.value} 得分 {first.score:.2f}, "
                    f"{second.category.value} 得分 {second.score:.2f}, 分差 {margin:.2f} "
                    f"小于 minimum_margin={self.rules.minimum_margin:.2f}。"
                    f"第一名命中: {_describe(first.matches)}; "
                    f"第二名命中: {_describe(second.matches)}。"
                ),
                provider=self.provider,
                matched_rules=first.matches + second.matches,
                is_ambiguous=True,
            )

        return ClassificationResult(
            category=first.category,
            score=first.score,
            reason=(
                f"规则选择 {first.category.value}: 得分 {first.score:.2f}, "
                f"领先 {second.category.value} {margin:.2f} 分; 命中: {_describe(first.matches)}。"
            ),
            provider=self.provider,
            matched_rules=first.matches,
        )

    def _fallback(
        self,
        default: Category | None,
        ranked: list[_RankedCategory],
        first: _RankedCategory,
    ) -> ClassificationResult:
        if default is not None and default is not Category.UNCLASSIFIED:
            default_candidate = next(
                candidate for candidate in ranked if candidate.category is default
            )
            return ClassificationResult(
                category=default,
                score=default_candidate.score,
                reason=(
                    f"最高规则分 {first.score:.2f} 未达到 minimum_score="
                    f"{self.rules.minimum_score:.2f}, 使用来源默认分类 {default.value}。"
                ),
                provider="source_default",
                matched_rules=default_candidate.matches,
            )
        return ClassificationResult(
            category=Category.UNCLASSIFIED,
            score=first.score,
            reason=(
                f"最高规则分 {first.score:.2f} 未达到 minimum_score="
                f"{self.rules.minimum_score:.2f}, 且来源无默认分类, 进入 unclassified。"
            ),
            provider=self.provider,
            matched_rules=first.matches,
        )


def _parse_source_default(value: Category | str | None) -> Category | None:
    if value is None or value == "":
        return None
    try:
        return Category(value)
    except ValueError as exc:
        raise ValueError(f"未知来源默认分类: {value}") from exc


def _contains(text: str, term: str) -> bool:
    if not term:
        return False
    if _ASCII_TERM.fullmatch(term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _positive_matches(
    text: str,
    phrases: Mapping[str, float],
    keywords: Mapping[str, float],
    *,
    weight: float,
    phrase_weight: float,
) -> tuple[tuple[str, str, float], ...]:
    candidates: list[tuple[str, str, str, float]] = []
    for rule_type, terms, multiplier in (
        ("phrase", phrases, phrase_weight),
        ("keyword", keywords, 1.0),
    ):
        for term, points in terms.items():
            normalized_term = normalize_text(term)
            if text and _contains(text, normalized_term):
                candidates.append((rule_type, term, normalized_term, points * weight * multiplier))

    # One textual span is one piece of evidence. Prefer the longest matching rule,
    # and prefer a phrase over an identical keyword, instead of stacking both.
    candidates.sort(key=lambda match: (len(match[2]), match[0] == "phrase"), reverse=True)
    selected: list[tuple[str, str, str, float]] = []
    for candidate in candidates:
        if any(candidate[2] in existing[2] for existing in selected):
            continue
        selected.append(candidate)
    return tuple((rule_type, term, contribution) for rule_type, term, _, contribution in selected)


def _term_matches(
    text: str,
    terms: Mapping[str, float],
    *,
    weight: float,
    multiplier: float,
    rule_type: str,
) -> tuple[tuple[str, str, float], ...]:
    return tuple(
        (rule_type, term, points * weight * multiplier)
        for term, points in terms.items()
        if text and _contains(text, normalize_text(term))
    )


def _describe(matches: tuple[str, ...]) -> str:
    return "、".join(matches) if matches else "无"
