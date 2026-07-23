"""Loading and strict validation for YAML classification rules."""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from app.domain.enums import Category
from app.utils.text import normalize_text

CLASSIFIABLE_CATEGORIES = tuple(
    category for category in Category if category is not Category.UNCLASSIFIED
)
_ROOT_KEYS = {"settings", "global_negative_phrases", "categories"}
_SETTING_KEYS = {
    "minimum_score",
    "minimum_margin",
    "title_weight",
    "summary_weight",
    "phrase_weight",
}
_CATEGORY_KEYS = {"priority", "phrases", "keywords", "negative_phrases"}


class RuleConfigError(ValueError):
    """Raised when the rule file is malformed or semantically invalid."""


@dataclass(frozen=True, slots=True)
class CategoryRules:
    priority: int
    phrases: Mapping[str, float]
    keywords: Mapping[str, float]
    negative_phrases: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ClassificationRules:
    minimum_score: float
    minimum_margin: float
    title_weight: float
    summary_weight: float
    phrase_weight: float
    global_negative_phrases: Mapping[str, float]
    categories: Mapping[Category, CategoryRules]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses silently overwritten mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, *, deep: bool = False
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(
            object,
            loader.construct_object(key_node, deep=deep),  # pyright: ignore[reportUnknownMemberType]
        )
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node,
                deep=deep,
            ),
        )
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_classification_rules(path: Path) -> ClassificationRules:
    """Load one YAML file and fail fast with an actionable configuration error."""

    try:
        with path.open(encoding="utf-8") as rule_file:
            raw = yaml.load(rule_file, Loader=_UniqueKeyLoader)
    except OSError as exc:
        raise RuleConfigError(f"无法读取分类规则 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise RuleConfigError(f"分类规则 YAML 格式错误 {path}: {exc}") from exc

    root = _mapping(raw, "根节点")
    _reject_unknown(root, _ROOT_KEYS, "根节点")
    settings = _mapping(root.get("settings"), "settings")
    _reject_unknown(settings, _SETTING_KEYS, "settings")
    missing_settings = _SETTING_KEYS.difference(settings)
    if missing_settings:
        raise RuleConfigError(f"settings 缺少字段: {', '.join(sorted(missing_settings))}")

    minimum_score = _number(settings["minimum_score"], "settings.minimum_score", minimum=0)
    minimum_margin = _number(settings["minimum_margin"], "settings.minimum_margin", minimum=0)
    title_weight = _number(settings["title_weight"], "settings.title_weight", exclusive_minimum=0)
    summary_weight = _number(
        settings["summary_weight"], "settings.summary_weight", exclusive_minimum=0
    )
    phrase_weight = _number(settings["phrase_weight"], "settings.phrase_weight", minimum=1)
    if title_weight <= summary_weight:
        raise RuleConfigError("settings.title_weight 必须大于 settings.summary_weight")

    if "global_negative_phrases" not in root:
        raise RuleConfigError("根节点缺少字段: global_negative_phrases")
    global_negative_phrases = _terms(
        root.get("global_negative_phrases"), "global_negative_phrases", negative=True
    )

    raw_categories = _mapping(root.get("categories"), "categories")
    allowed_names = {category.value for category in CLASSIFIABLE_CATEGORIES}
    unknown_names = set(raw_categories).difference(allowed_names)
    if unknown_names:
        raise RuleConfigError(f"categories 包含未知分类: {', '.join(sorted(unknown_names))}")
    missing_names = allowed_names.difference(raw_categories)
    if missing_names:
        raise RuleConfigError(f"categories 缺少分类: {', '.join(sorted(missing_names))}")

    categories: dict[Category, CategoryRules] = {}
    for category in CLASSIFIABLE_CATEGORIES:
        location = f"categories.{category.value}"
        raw_rule = _mapping(raw_categories[category.value], location)
        _reject_unknown(raw_rule, _CATEGORY_KEYS, location)
        missing = _CATEGORY_KEYS.difference(raw_rule)
        if missing:
            raise RuleConfigError(f"{location} 缺少字段: {', '.join(sorted(missing))}")
        priority = raw_rule["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise RuleConfigError(f"{location}.priority 必须是整数")
        categories[category] = CategoryRules(
            priority=priority,
            phrases=_terms(raw_rule["phrases"], f"{location}.phrases", negative=False),
            keywords=_terms(raw_rule["keywords"], f"{location}.keywords", negative=False),
            negative_phrases=_terms(
                raw_rule["negative_phrases"], f"{location}.negative_phrases", negative=True
            ),
        )

    return ClassificationRules(
        minimum_score=minimum_score,
        minimum_margin=minimum_margin,
        title_weight=title_weight,
        summary_weight=summary_weight,
        phrase_weight=phrase_weight,
        global_negative_phrases=global_negative_phrases,
        categories=categories,
    )


def _mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuleConfigError(f"{location} 必须是字符串键的映射")
    raw_mapping = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise RuleConfigError(f"{location} 必须是字符串键的映射")
    return cast(Mapping[str, Any], raw_mapping)


def _reject_unknown(value: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = set(value).difference(allowed)
    if unknown:
        raise RuleConfigError(f"{location} 包含未知字段: {', '.join(sorted(unknown))}")


def _number(
    value: object,
    location: str,
    *,
    minimum: float | None = None,
    exclusive_minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuleConfigError(f"{location} 必须是数值")
    result = float(value)
    if not math.isfinite(result):
        raise RuleConfigError(f"{location} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise RuleConfigError(f"{location} 必须大于等于 {minimum:g}")
    if exclusive_minimum is not None and result <= exclusive_minimum:
        raise RuleConfigError(f"{location} 必须大于 {exclusive_minimum:g}")
    return result


def _terms(value: object, location: str, *, negative: bool) -> Mapping[str, float]:
    raw_terms = _mapping(value, location)
    terms: dict[str, float] = {}
    normalized_terms: dict[str, str] = {}
    for term, raw_score in raw_terms.items():
        if not term.strip():
            raise RuleConfigError(f"{location} 不允许空规则")
        normalized_term = normalize_text(term)
        if not normalized_term:
            raise RuleConfigError(f"{location}.{term} 规范化后不能为空")
        previous = normalized_terms.get(normalized_term)
        if previous is not None:
            raise RuleConfigError(f"{location}.{term} 与 {location}.{previous} 规范化后重复")
        score = _number(raw_score, f"{location}.{term}")
        if negative and score >= 0:
            raise RuleConfigError(f"{location}.{term} 必须是负数")
        if not negative and score <= 0:
            raise RuleConfigError(f"{location}.{term} 必须是正数")
        terms[term] = score
        normalized_terms[normalized_term] = term
    return terms
