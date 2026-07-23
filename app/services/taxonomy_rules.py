"""Strict, role-scoped taxonomy-v2 rule loading and deterministic matching."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from app.config.settings import PROJECT_ROOT
from app.domain.enums import IndustryTag, PrimaryType, SourceRole, TopicTag

RULE_ROOT = PROJECT_ROOT / "app" / "config" / "classification"
_ROOT_KEYS = {"version", "role", "rules"}
_RULE_KEYS = {
    "id",
    "priority",
    "primary_type",
    "subject_terms",
    "action_terms",
    "exclude_terms",
    "topic_tags",
}


class TaxonomyRuleError(ValueError):
    """Raised when a role rule pack is not strict, complete or deterministic."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueLoader, node: MappingNode, *, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                key_node, deep=deep
            ),
        )
        if key in result:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = cast(
            object,
            loader.construct_object(  # pyright: ignore[reportUnknownMemberType]
                value_node, deep=deep
            ),
        )
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True, slots=True)
class TaxonomyRule:
    rule_id: str
    priority: int
    primary_type: PrimaryType
    subject_terms: tuple[str, ...]
    action_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]
    topic_tags: tuple[TopicTag, ...]


@dataclass(frozen=True, slots=True)
class RoleRulePack:
    version: str
    role: SourceRole
    rules: tuple[TaxonomyRule, ...]


@dataclass(frozen=True, slots=True)
class TagRules:
    topic_tags: Mapping[TopicTag, tuple[str, ...]]
    industry_tags: Mapping[IndustryTag, tuple[str, ...]]


def load_role_rule_pack(role: SourceRole, root: Path = RULE_ROOT) -> RoleRulePack:
    if role is SourceRole.FALLBACK:
        return RoleRulePack("v2", role, ())
    path = root / f"{role.value}.yaml"
    raw = _load(path)
    _exact_keys(raw, _ROOT_KEYS, str(path))
    version = _text(raw["version"], "version")
    if version != "v2":
        raise TaxonomyRuleError(f"{path}: version must be v2")
    parsed_role = SourceRole(_text(raw["role"], "role"))
    if parsed_role is not role:
        raise TaxonomyRuleError(f"{path}: role does not match filename")
    raw_rules = _sequence(raw["rules"], "rules")
    seen_ids: set[str] = set()
    rules: list[TaxonomyRule] = []
    for index, raw_rule in enumerate(raw_rules, start=1):
        mapping = _mapping(raw_rule, f"rules[{index}]")
        _exact_keys(mapping, _RULE_KEYS, f"rules[{index}]")
        rule_id = _text(mapping["id"], f"rules[{index}].id")
        if rule_id in seen_ids:
            raise TaxonomyRuleError(f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)
        priority = mapping["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise TaxonomyRuleError(f"rules[{index}].priority must be an integer")
        rules.append(
            TaxonomyRule(
                rule_id=rule_id,
                priority=priority,
                primary_type=PrimaryType(_text(mapping["primary_type"], "primary_type")),
                subject_terms=_text_list(mapping["subject_terms"], "subject_terms"),
                action_terms=_text_list(mapping["action_terms"], "action_terms"),
                exclude_terms=_text_list(mapping["exclude_terms"], "exclude_terms"),
                topic_tags=tuple(
                    TopicTag(value) for value in _text_list(mapping["topic_tags"], "topic_tags")
                ),
            )
        )
    return RoleRulePack(version, role, tuple(sorted(rules, key=lambda rule: -rule.priority)))


def load_tag_rules(root: Path = RULE_ROOT) -> TagRules:
    raw = _load(root / "tags.yaml")
    _exact_keys(raw, {"version", "topic_tags", "industry_tags"}, "tags")
    if _text(raw["version"], "version") != "v2":
        raise TaxonomyRuleError("tags.version must be v2")
    topic = _tag_mapping(raw["topic_tags"], TopicTag, "topic_tags")
    industry = _tag_mapping(raw["industry_tags"], IndustryTag, "industry_tags")
    if set(topic) != set(TopicTag):
        raise TaxonomyRuleError("topic_tags must define every allowed tag exactly once")
    if set(industry) != set(IndustryTag) - {IndustryTag.GENERAL}:
        raise TaxonomyRuleError("industry_tags must define every specific allowed tag")
    return TagRules(topic, industry)


def _load(path: Path) -> Mapping[str, object]:
    try:
        with path.open(encoding="utf-8") as stream:
            return _mapping(yaml.load(stream, Loader=_UniqueLoader), str(path))
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyRuleError(f"cannot load taxonomy rules {path}: {exc}") from exc


def _mapping(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TaxonomyRuleError(f"{location} must be a string-key mapping")
    result: dict[str, object] = {}
    raw = cast(Mapping[object, object], value)
    for key, item in raw.items():
        if not isinstance(key, str):
            raise TaxonomyRuleError(f"{location} must be a string-key mapping")
        result[key] = item
    return result


def _sequence(value: object, location: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TaxonomyRuleError(f"{location} must be a list")
    return cast(Sequence[object], value)


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaxonomyRuleError(f"{location} must be non-empty text")
    return value.strip()


def _text_list(value: object, location: str) -> tuple[str, ...]:
    result = tuple(_text(entry, location) for entry in _sequence(value, location))
    normalized = [entry.casefold() for entry in result]
    if len(normalized) != len(set(normalized)):
        raise TaxonomyRuleError(f"{location} contains duplicate entries")
    return result


def _exact_keys(value: Mapping[str, object], expected: set[str], location: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise TaxonomyRuleError(
            f"{location} schema mismatch; missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _tag_mapping[TagT: TopicTag | IndustryTag](
    value: object, enum_type: type[TagT], location: str
) -> Mapping[TagT, tuple[str, ...]]:
    mapping = _mapping(value, location)
    result: dict[TagT, tuple[str, ...]] = {}
    for key, raw_terms in mapping.items():
        result[enum_type(key)] = _text_list(raw_terms, f"{location}.{key}")
    return result
