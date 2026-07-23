"""Deterministic taxonomy-v2 classification separated from admission/publication."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.domain.collection import CollectedItem
from app.domain.enums import (
    CaseCompleteness,
    IndustryTag,
    PrimaryType,
    SourceRole,
    TopicTag,
)
from app.domain.taxonomy import TAXONOMY_VERSION, OpportunityFields, TaxonomyResult
from app.services.taxonomy_rules import (
    RoleRulePack,
    TagRules,
    load_role_rule_pack,
    load_tag_rules,
)
from app.utils.text import normalize_text
from app.utils.url import canonicalize_url

_DATE = re.compile(
    r"(?P<year>20\d{2})[年./-](?P<month>0?[1-9]|1[0-2])[月./-](?P<day>0?[1-9]|[12]\d|3[01])日?"
)
_QUANTIFIED = re.compile(r"\d+(?:\.\d+)?\s*(?:%|倍|万|亿|小时|分钟|天|元|人)")


class TaxonomyClassificationService:
    """Choose one information form, validated tags and opportunity fields."""

    def __init__(self) -> None:
        self._packs: dict[SourceRole, RoleRulePack] = {
            role: load_role_rule_pack(role) for role in SourceRole
        }
        self._tag_rules: TagRules = load_tag_rules()

    def classify(self, item: CollectedItem, source_role: SourceRole) -> TaxonomyResult:
        title = normalize_text(item.title)
        summary = normalize_text(item.summary)
        content = f"{title} {summary}".strip()
        matches = [
            rule
            for rule in self._packs[source_role].rules
            if _matches_rule(
                content,
                subject_terms=rule.subject_terms,
                action_terms=rule.action_terms,
                exclude_terms=rule.exclude_terms,
            )
        ]
        matched_rules: list[str] = []
        primary_type = PrimaryType.UNCLASSIFIED
        score = 0.0
        if matches:
            top_priority = matches[0].priority
            top = [rule for rule in matches if rule.priority == top_priority]
            matched_rules.extend(rule.rule_id for rule in top)
            top_types = {rule.primary_type for rule in top}
            if len(top_types) == 1:
                primary_type = top[0].primary_type
                score = min(1.0, 0.6 + top_priority / 250)
            else:
                matched_rules.append("taxonomy.conflict_safe_unclassified")

        topic_tags = _stable_tags(content, self._tag_rules.topic_tags, TopicTag)
        for rule in matches:
            topic_tags = _stable_union(topic_tags, rule.topic_tags, TopicTag)
        industry_tags = _stable_tags(content, self._tag_rules.industry_tags, IndustryTag)
        case_completeness, case_rules = _case_completeness(item, primary_type)
        matched_rules.extend(case_rules)
        opportunity = (
            _opportunity_fields(item)
            if primary_type is PrimaryType.APPLICATION_OPPORTUNITY
            else OpportunityFields()
        )
        if primary_type is PrimaryType.UNCLASSIFIED:
            reason = "没有唯一且足够明确的 taxonomy-v2 事件动作, 安全回退 unclassified。"
        else:
            rules = ", ".join(matched_rules)
            reason = f"按来源角色 {source_role.value} 命中 {primary_type.value}: {rules}"
        return TaxonomyResult(
            primary_type=primary_type,
            topic_tags=topic_tags,
            industry_tags=industry_tags,
            case_completeness=case_completeness,
            opportunity=opportunity,
            taxonomy_version=TAXONOMY_VERSION,
            matched_rules=tuple(dict.fromkeys(matched_rules)),
            reason=reason,
            score=score,
        )


def _matches_rule(
    content: str,
    *,
    subject_terms: tuple[str, ...],
    action_terms: tuple[str, ...],
    exclude_terms: tuple[str, ...],
) -> bool:
    normalized_subjects = tuple(normalize_text(term) for term in subject_terms)
    normalized_actions = tuple(normalize_text(term) for term in action_terms)
    normalized_excludes = tuple(normalize_text(term) for term in exclude_terms)
    if any(term and term in content for term in normalized_excludes):
        return False
    subject_ok = not normalized_subjects or any(term in content for term in normalized_subjects)
    action_ok = not normalized_actions or any(term in content for term in normalized_actions)
    return subject_ok and action_ok


def _stable_tags[TagT: TopicTag | IndustryTag](
    content: str,
    rules: Mapping[TagT, tuple[str, ...]],
    enum_type: type[TagT],
) -> tuple[TagT, ...]:
    matched = {
        tag
        for tag, terms in rules.items()
        if any(normalize_text(term) in content for term in terms)
    }
    return tuple(tag for tag in enum_type if tag in matched)


def _stable_union[TagT: TopicTag | IndustryTag](
    first: tuple[TagT, ...], second: tuple[TagT, ...], enum_type: type[TagT]
) -> tuple[TagT, ...]:
    values = set(first) | set(second)
    return tuple(tag for tag in enum_type if tag in values)


def _case_completeness(
    item: CollectedItem, primary_type: PrimaryType
) -> tuple[CaseCompleteness, tuple[str, ...]]:
    extra = item.extra
    raw_detail = next(
        (
            value
            for key in ("content", "body", "detail_text")
            if isinstance((value := extra.get(key)), str) and value.strip()
        ),
        None,
    )
    summary_text = normalize_text(item.summary)
    title_text = normalize_text(item.title)
    has_case_signal = primary_type in {PrimaryType.CASE_ANALYSIS, PrimaryType.AWARD_RESULT} and (
        "案例" in f"{title_text} {summary_text}" or primary_type is PrimaryType.CASE_ANALYSIS
    )
    if raw_detail is None or extra.get("detail_fetched") is not True:
        if has_case_signal:
            return CaseCompleteness.CASE_LEAD, ("case.title_or_summary_ceiling",)
        return CaseCompleteness.NOT_CASE, ()

    detail = normalize_text(raw_detail)
    dimensions = {
        "A": any(term in detail for term in ("背景", "痛点", "面临", "业务需求")),
        "B": any(term in detail for term in ("方案", "架构", "技术路线", "建设")),
        "C": any(term in detail for term in ("实施", "部署", "主要做法", "上线", "流程")),
        "D": any(term in detail for term in ("应用结果", "成效", "效果", "提升", "降低", "节省")),
        "E": bool(_QUANTIFIED.search(detail))
        and any(term in detail for term in ("提升", "降低", "节省", "减少", "增长", "达到")),
    }
    matched = tuple(f"case.dimension_{key}" for key, value in dimensions.items() if value)
    if sum(dimensions.values()) >= 3 and (dimensions["D"] or dimensions["E"]):
        return CaseCompleteness.FULL_CASE, matched
    if has_case_signal and any(dimensions[key] for key in ("A", "B", "C")):
        return CaseCompleteness.PARTIAL_CASE, matched
    if has_case_signal:
        return CaseCompleteness.CASE_LEAD, matched
    return CaseCompleteness.NOT_CASE, matched


def _opportunity_fields(item: CollectedItem) -> OpportunityFields:
    extra = item.extra
    text = "\n".join(value for value in (item.title, item.summary or "") if value)
    organizer = _bounded_extra_text(extra.get("organizer"), 500) or _label_value(text, "主办机构")
    application_name = _bounded_extra_text(extra.get("application_name"), 500) or item.title[:500]
    application_target = _bounded_extra_text(
        extra.get("application_target"), 2_000
    ) or _label_value(text, "申报对象")
    application_method = _bounded_extra_text(
        extra.get("application_method"), 2_000
    ) or _label_value(text, "申报方式")
    raw_url = extra.get("application_url")
    application_url = canonicalize_url(raw_url) if isinstance(raw_url, str) else None
    deadline = _explicit_deadline(text)
    return OpportunityFields(
        organizer=organizer,
        application_name=application_name,
        application_target=application_target,
        deadline_at=deadline,
        application_method=application_method,
        application_url=application_url,
    )


def _explicit_deadline(text: str) -> datetime | None:
    deadline_context = re.search(r"(?:截止(?:时间|日期)?|申报截止)[^。；;\n]{0,30}", text)  # noqa: RUF001
    if deadline_context is None or (match := _DATE.search(deadline_context.group(0))) is None:
        return None
    try:
        local = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            23,
            59,
            59,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    except ValueError:
        return None
    return local.astimezone(UTC)


def _label_value(text: str, label: str) -> str | None:
    match = re.search(
        rf"{re.escape(label)}\s*[:：]\s*([^。；;\n]{{1,500}})",  # noqa: RUF001
        text,
    )
    return match.group(1).strip() if match else None


def _bounded_extra_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] or None
