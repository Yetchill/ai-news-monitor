"""Fixed-corpus and boundary tests for the pure classification system."""

from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from app.classifiers import (
    FinalCategoryResolver,
    HybridClassifier,
    LLMClassifier,
    ManualCategoryError,
    RuleBasedClassifier,
    RuleConfigError,
    load_classification_rules,
)
from app.classifiers.providers import DeepSeekProvider
from app.config import get_settings
from app.domain.classification import ClassificationResult
from app.domain.collection import CollectedItem
from app.domain.enums import Category
from app.utils.text import normalize_text

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "classification_cases.yaml"
ADVERSARIAL_FIXTURE_PATH = (
    Path(__file__).parents[1] / "fixtures" / "classification_adversarial_cases.yaml"
)


def _item(title: str, summary: str | None = None) -> CollectedItem:
    return CollectedItem(
        title=title,
        summary=summary,
        original_url="https://example.com/item",
        canonical_url="https://example.com/item",
    )


def _cases(path: Path = FIXTURE_PATH) -> list[dict[str, Any]]:
    raw_object: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw_object, dict)
    raw = cast(dict[str, object], raw_object)
    cases = raw["cases"]
    assert isinstance(cases, list)
    return cast(list[dict[str, Any]], cases)


def _accuracy(correct: Counter[str], total: Counter[str], category: str) -> float:
    return correct[category] / total[category] if total[category] else 0.0


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
async def test_fixed_classification_case(case: dict[str, Any]) -> None:
    classifier = RuleBasedClassifier.from_yaml()
    result = await classifier.classify(
        _item(str(case["title"]), cast(str | None, case.get("summary"))),
        source_default=cast(str | None, case.get("source_default")),
    )

    assert result.category.value == case["expected"], result.reason
    assert result.is_ambiguous is bool(case.get("expected_ambiguous", False))
    assert result.reason


async def test_fixed_corpus_accuracy_and_confusion_report() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    cases = _cases()
    correct_by_expected: Counter[str] = Counter()
    total_by_expected: Counter[str] = Counter()
    confusion: list[str] = []

    for case in cases:
        result = await classifier.classify(
            _item(str(case["title"]), cast(str | None, case.get("summary"))),
            source_default=cast(str | None, case.get("source_default")),
        )
        expected = str(case["expected"])
        total_by_expected[expected] += 1
        if result.category.value == expected:
            correct_by_expected[expected] += 1
        else:
            confusion.append(f"{case['id']}: {expected} -> {result.category.value}")

    total_correct = sum(correct_by_expected.values())
    accuracy = total_correct / len(cases)
    print(f"classification accuracy: {total_correct}/{len(cases)} = {accuracy:.2%}")
    for category in Category:
        expected_total = total_by_expected[category.value]
        print(
            f"{category.value}: {correct_by_expected[category.value]}/{expected_total} "
            f"= {correct_by_expected[category.value] / expected_total:.2%}"
        )
    print(f"confusion: {confusion or 'none'}")

    assert accuracy >= 0.90
    for category in (Category.SOLICITATION, Category.AWARD_CASE):
        assert correct_by_expected[category.value] / total_by_expected[category.value] >= 0.90


async def test_adversarial_corpus_accuracy_and_confusion_report() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    cases = _cases(ADVERSARIAL_FIXTURE_PATH)
    fixed_titles = {str(case["title"]) for case in _cases()}
    assert len(cases) >= 80
    assert len({str(case["id"]) for case in cases}) == len(cases)
    assert not fixed_titles.intersection(str(case["title"]) for case in cases)

    correct_by_expected: Counter[str] = Counter()
    total_by_expected: Counter[str] = Counter()
    errors: list[str] = []
    for case in cases:
        result = await classifier.classify(
            _item(str(case["title"]), cast(str | None, case.get("summary"))),
            source_default=cast(str | None, case.get("source_default")),
        )
        expected = str(case["expected"])
        total_by_expected[expected] += 1
        if result.category.value == expected:
            correct_by_expected[expected] += 1
        else:
            errors.append(
                f"{case['id']}: {expected} -> {result.category.value}; "
                f"score={result.score:.2f}; reason={result.reason}; "
                f"matched_rules={result.matched_rules}"
            )

    total_correct = sum(correct_by_expected.values())
    accuracy = total_correct / len(cases)
    print(f"adversarial accuracy: {total_correct}/{len(cases)} = {accuracy:.2%}")
    for category in Category:
        category_accuracy = _accuracy(correct_by_expected, total_by_expected, category.value)
        print(
            f"{category.value}: {correct_by_expected[category.value]}/"
            f"{total_by_expected[category.value]} = {category_accuracy:.2%}"
        )
    print("errors:\n" + ("\n".join(errors) if errors else "none"))

    assert accuracy >= 0.85
    for category in (Category.SOLICITATION, Category.AWARD_CASE):
        assert _accuracy(correct_by_expected, total_by_expected, category.value) >= 0.85


async def test_manual_category_always_overrides_automatic_result() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    automatic = await classifier.classify(_item("某公司发布新一代大模型"))
    original = deepcopy(automatic)

    final = FinalCategoryResolver().resolve(automatic, manual_category=Category.SOLICITATION)

    assert final.category is Category.SOLICITATION
    assert final.provider == "manual"
    assert "最高优先级" in final.reason
    assert automatic == original


def test_manual_category_rejects_unknown_value() -> None:
    automatic = ClassificationResult(Category.UNCLASSIFIED, 0, "none", "rule_based")
    with pytest.raises(ManualCategoryError, match="未知人工分类"):
        FinalCategoryResolver().resolve(automatic, manual_category="other")


async def test_classifier_does_not_mutate_collected_item() -> None:
    item = _item("智能体平台2.0正式上线", "新增多个功能")
    original = deepcopy(item)
    await RuleBasedClassifier.from_yaml().classify(item)
    assert item == original


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  智能体　平台\n2.0  ", "智能体 平台 2.0"),
        ("\uff21\uff47\uff45\uff4e\uff54 平台", "agent 平台"),
        ("模型\uff0c发布\uff01V2.1", "模型,发布!v2.1"),
        ("大模型\uff08增强版\uff09", "大模型(增强版)"),
    ],
)
def test_text_normalization(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\uff21\uff29\uff0bRAG", "ai+rag"),
        ("Agent 2\uff0e0", "agent 2.0"),
        ("GitHub Release V1\uff0e2-RC1", "github release v1.2-rc1"),
        ("SDK\uff0fLLM", "sdk/llm"),
    ],
)
def test_normalization_preserves_technical_names(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


async def test_repeated_term_and_nested_keyword_do_not_amplify_score() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    single = await classifier.classify(_item("大模型推理能力评测"))
    repeated = await classifier.classify(_item("大模型大模型大模型推理能力评测"))

    assert repeated.score == single.score
    assert repeated.matched_rules == single.matched_rules
    assert any("phrase:大模型" in match for match in repeated.matched_rules)
    assert not any("keyword:模型" in match for match in repeated.matched_rules)


async def test_ascii_keyword_does_not_match_inside_longer_word() -> None:
    result = await RuleBasedClassifier.from_yaml().classify(_item("Reagent platform update"))
    assert result.category is Category.UNCLASSIFIED


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        (cast(str, None), None),
        ("", None),
        ("\uff01\uff1f……---", None),
        ("普通内部通知" * 20_000, None),
    ],
)
async def test_missing_empty_symbol_only_and_long_titles_are_safe(
    title: str, summary: str | None
) -> None:
    result = await RuleBasedClassifier.from_yaml().classify(_item(title, summary))
    assert result.category is Category.UNCLASSIFIED
    assert result.reason


async def test_source_default_is_fallback_only() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    strong = await classifier.classify(
        _item("推理模型披露训练架构"), source_default=Category.AWARD_CASE
    )
    ambiguous = await classifier.classify(
        _item("智能体大模型"),
        source_default=Category.MODEL_TECHNOLOGY,
    )
    weak = await classifier.classify(
        _item("中心例行工作动态"), source_default=Category.POLICY_INDUSTRY
    )

    assert strong.category is Category.MODEL_TECHNOLOGY
    assert strong.provider == "rule_based"
    assert ambiguous.category is Category.AGENT_PRODUCT
    assert ambiguous.provider == "rule_based"
    assert ambiguous.score >= 8
    assert weak.category is Category.POLICY_INDUSTRY
    assert weak.provider == "source_default"


async def test_reason_and_matches_are_stable_and_category_qualified() -> None:
    classifier = RuleBasedClassifier.from_yaml()
    first = await classifier.classify(_item("视觉语言模型完成预训练"))
    second = await classifier.classify(_item("视觉语言模型完成预训练"))
    assert first == second
    assert first.matched_rules
    assert all(match.startswith(f"{first.category.value}.") for match in first.matched_rules)


async def test_llm_and_hybrid_classifiers_are_importable_and_constructable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify LLMClassifier and HybridClassifier are real implementations, not placeholders."""
    assert LLMClassifier.provider == "llm"
    assert HybridClassifier.provider == "hybrid"

    monkeypatch.setenv("AIM_LLM_API_KEY", "test-only-key")
    get_settings.cache_clear()
    provider = DeepSeekProvider()
    get_settings.cache_clear()
    llm = LLMClassifier(provider)
    assert llm.provider == "llm"

    rule = RuleBasedClassifier.from_yaml()
    hybrid = HybridClassifier(rule, provider)
    assert hybrid.provider == "hybrid"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            "settings: {}\ncategories:\n  invented: {}\n",
            "settings 缺少字段",
        ),
        (
            """
settings:
  minimum_score: ten
  minimum_margin: 3
  title_weight: 2
  summary_weight: 1
  phrase_weight: 1.5
global_negative_phrases: {}
categories: {}
""",
            "minimum_score 必须是数值",
        ),
        ("settings: [broken", "YAML 格式错误"),
    ],
)
def test_rule_loader_reports_clear_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RuleConfigError, match=message):
        load_classification_rules(path)


def test_rule_loader_rejects_unknown_category(tmp_path: Path) -> None:
    valid = yaml.safe_load(
        (Path(__file__).parents[2] / "app/config/classification_rules.yaml").read_text(
            encoding="utf-8"
        )
    )
    valid["categories"]["invented"] = {
        "priority": 1,
        "phrases": {},
        "keywords": {},
        "negative_phrases": {},
    }
    path = tmp_path / "unknown.yaml"
    path.write_text(yaml.safe_dump(valid, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RuleConfigError, match="未知分类: invented"):
        load_classification_rules(path)


def test_rule_loader_rejects_duplicate_yaml_key_with_location(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text("settings:\n  minimum_score: 8\n  minimum_score: 9\n", encoding="utf-8")
    with pytest.raises(RuleConfigError, match=r"(?s)duplicate key.*line 3, column 3"):
        load_classification_rules(path)


def test_rule_loader_rejects_terms_that_normalize_to_same_value(tmp_path: Path) -> None:
    valid = yaml.safe_load(
        (Path(__file__).parents[2] / "app/config/classification_rules.yaml").read_text(
            encoding="utf-8"
        )
    )
    valid["categories"]["model_technology"]["phrases"]["\uff21\uff27\uff25\uff2e\uff34"] = 3
    valid["categories"]["model_technology"]["phrases"]["agent"] = 4
    path = tmp_path / "normalized-duplicate.yaml"
    path.write_text(yaml.safe_dump(valid, allow_unicode=True), encoding="utf-8")
    with pytest.raises(RuleConfigError, match="规范化后重复"):
        load_classification_rules(path)


async def test_priority_cannot_override_a_real_score_difference(tmp_path: Path) -> None:
    valid = yaml.safe_load(
        (Path(__file__).parents[2] / "app/config/classification_rules.yaml").read_text(
            encoding="utf-8"
        )
    )
    valid["settings"]["minimum_score"] = 1
    valid["settings"]["minimum_margin"] = 1
    valid["global_negative_phrases"] = {}
    for rules in valid["categories"].values():
        rules["phrases"] = {}
        rules["keywords"] = {}
        rules["negative_phrases"] = {}
    valid["categories"]["model_technology"]["keywords"] = {"alpha": 2}
    valid["categories"]["solicitation"]["keywords"] = {"beta": 1}
    path = tmp_path / "priority.yaml"
    path.write_text(yaml.safe_dump(valid, allow_unicode=True), encoding="utf-8")

    result = await RuleBasedClassifier.from_yaml(path).classify(_item("alpha beta"))
    assert result.category is Category.MODEL_TECHNOLOGY


async def test_unknown_source_default_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知来源默认分类"):
        await RuleBasedClassifier.from_yaml().classify(_item("普通消息"), source_default="invented")
