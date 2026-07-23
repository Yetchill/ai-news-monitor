"""Deterministic content admission between normalization and classification."""

import math
import re
from collections.abc import Iterable
from typing import cast
from urllib.parse import urlsplit

from app.domain.admission import AdmissionResult, AdmissionRuleMatch
from app.domain.collection import CollectedItem
from app.domain.enums import Category, PrimaryType, SourceKind, SourceRole, SourceTier, SourceType
from app.domain.models import Source

_REJECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recruitment", re.compile(r"招聘|校招|社招|实习生招聘|诚聘")),
    ("training_promotion", re.compile(r"培训班|培训招生|课程推广|课程报名|考证|证书宣传")),
    ("membership_promotion", re.compile(r"会员招募|会员服务|入会申请|招募会员")),
    ("commercial_promotion", re.compile(r"优惠活动|限时优惠|折扣|促销|展会招商|会议售票")),
    ("event_recap", re.compile(r"活动回顾|会议回顾|大会回顾|精彩回顾|圆满举办|圆满落幕")),
    ("event_preview", re.compile(r"活动预告|活动预热|嘉宾介绍|嘉宾阵容|会议日程|大会日程")),
    ("routine_visit", re.compile(r"领导参观|领导一行莅临|到访参观|考察交流")),
    ("empty_cooperation", re.compile(r"签约仪式|签约合影|战略签约$")),
)

_STRUCTURAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("navigation_page", re.compile(r"^(首页|导航|更多|新闻中心|通知公告|产品中心|案例中心)$")),
    ("login_page", re.compile(r"登录|注册|验证码")),
    ("contact_page", re.compile(r"联系我们|联系方式")),
)

_POSITIVE_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    (
        "major_model_release",
        re.compile(
            r"(?:大模型|模型|LLM).{0,18}(?:正式发布|发布|上线|重大升级|新版本)|(?:正式发布|推出|上线).{0,18}(?:大模型|模型)"
        ),
        35,
    ),
    (
        "agent_product_release",
        re.compile(
            r"(?:智能体|Agent|agent).{0,20}(?:正式发布|发布|上线|平台|重大升级|能力升级)|(?:正式发布|推出|上线).{0,18}(?:智能体|Agent|agent)"
        ),
        35,
    ),
    (
        "policy_or_standard",
        re.compile(r"政策|行动计划|实施意见|管理办法|国家标准|行业标准|技术规范|标准发布"),
        30,
    ),
    (
        "solicitation_or_application",
        re.compile(r"征集|申报|试点申报|项目申报|奖项申报|参评通知"),
        35,
    ),
    (
        "authoritative_list",
        re.compile(r"入选名单|获奖名单|优秀案例|典型案例|示范案例|示范项目|推荐目录|评选结果|榜单"),
        35,
    ),
    ("enterprise_outcome", re.compile(r"落地|应用成果|实施成效|业务价值|降本增效|客户案例"), 30),
)

_AI_RELEVANCE = re.compile(
    r"人工智能|大模型|基础模型|生成式AI|生成式人工智能|智能体|Agent|LLM|多模态|模型平台|AI\+?"
)
_GITHUB_MAINTENANCE = re.compile(
    r"bug\s*fix|fix(?:es|ed)?\b|patch|dependency|dependencies|依赖升级|构建修复|CI/CD|文档修正|changelog|维护版本",
    re.IGNORECASE,
)
_GITHUB_PRERELEASE = re.compile(
    r"(?:^|[.\-_\s])(?:alpha|beta|rc|nightly|pre[- ]?release)(?:$|[.\-_\s\d])", re.IGNORECASE
)
_SEMVER = re.compile(r"(?:^|\s|v)(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")
_MEETING = re.compile(r"会议|大会|峰会|论坛|演讲|展台|参展|亮相|嘉宾")
_SUBSTANTIVE_EVENT = re.compile(
    r"正式发布|发布(?:了|新)?|上线|重大升级|开源|退役|下线|政策|法规|标准|征求意见|"
    r"征集|申报|报名|获奖|入选|名单|公示|白皮书|研究报告|行业报告"
)
_PRODUCT_DISALLOWED = re.compile(
    r"峰会演讲|参展|展台|亮相|战略签约|普通合作|融资|股价|人物采访|招聘|使用教程|优惠|促销"
)
_POLICY_DISALLOWED = re.compile(r"培训|调研|领导活动|座谈会")
_INDUSTRY_RELEVANCE = re.compile(
    r"人工智能|大模型|智能体|Agent|高质量数据集|算力|数据.{0,5}AI|AI.{0,5}应用|AI.{0,5}治理",
    re.IGNORECASE,
)


class BasicAdmissionPolicy:
    """Make auditable editorial admission decisions without classifying items."""

    def validate_source(self, source: Source) -> None:
        """Fail before collection when persisted admission configuration is invalid."""

        _validated_config(source)

    def admit(self, item: CollectedItem, source: Source) -> AdmissionResult:
        try:
            config = _validated_config(source)
        except ValueError as exc:
            return _decision(
                False,
                "source.configuration_invalid",
                [AdmissionRuleMatch("source.configuration_invalid", "reject", "source", str(exc))],
                0,
            )

        title = item.title.strip()
        if not title:
            return _hard_reject("content.empty_title", "title")
        if not _valid_url(item.original_url):
            return _hard_reject("content.invalid_url", "original_url")

        source_host = urlsplit(source.start_url).hostname
        item_host = urlsplit(item.original_url).hostname
        external_link = bool(source_host and item_host and source_host != item_host)
        if external_link and not config.allow_external_links:
            return _hard_reject("content.external_link_not_allowed", "original_url", item_host)
        if re.search(
            r"/(?:login|signin|register|contact)(?:/|$)",
            urlsplit(item.original_url).path,
            re.IGNORECASE,
        ):
            return _hard_reject("content.invalid_page_url", "original_url")

        for rule_id, pattern in _STRUCTURAL_PATTERNS:
            match = pattern.search(title)
            if match:
                return _hard_reject(f"content.{rule_id}", "title", match.group(0))

        title_and_summary = f"{title}\n{item.summary or ''}"
        for term in config.exclude_terms:
            if term.casefold() in title_and_summary.casefold():
                return _hard_reject("source.exclude_term", "content", term)

        for rule_id, pattern in _REJECT_PATTERNS:
            match = pattern.search(title_and_summary)
            if match:
                return _hard_reject(f"content.{rule_id}", "title", match.group(0))

        if _MEETING.search(title_and_summary) and not _SUBSTANTIVE_EVENT.search(title_and_summary):
            return _hard_reject("content.ordinary_meeting", "content")
        role_rejection = _role_rejection(title_and_summary, source.source_role)
        if role_rejection is not None:
            return role_rejection

        if source.source_type == SourceType.GITHUB_RELEASE:
            github_rejection = _github_rejection(title_and_summary, config.allow_technical_updates)
            if github_rejection is not None:
                return github_rejection

        include_matches = [
            term for term in config.include_terms if term.casefold() in title_and_summary.casefold()
        ]
        if config.include_terms and not include_matches:
            return _hard_reject("source.include_term_missing", "content")

        matches: list[AdmissionRuleMatch] = []
        score = 15
        matches.append(AdmissionRuleMatch("quality.base", "score", "content", score_delta=15))
        if external_link:
            matches.append(
                AdmissionRuleMatch(
                    "content.external_link_allowed", "score", "original_url", item_host
                )
            )
        if source.source_kind == SourceKind.FORMAL:
            score += 10
            matches.append(
                AdmissionRuleMatch("source.formal", "score", "source_kind", score_delta=10)
            )
        tier_bonus = {
            SourceTier.GOVERNMENT: 10,
            SourceTier.OFFICIAL_COMPANY: 8,
            SourceTier.ASSOCIATION: 7,
            SourceTier.AUTHORITATIVE_MEDIA: 5,
            SourceTier.FALLBACK: 0,
        }.get(source.source_tier, 0)
        if tier_bonus:
            score += tier_bonus
            matches.append(
                AdmissionRuleMatch(
                    "source.authority_tier",
                    "score",
                    "source_tier",
                    str(source.source_tier),
                    tier_bonus,
                )
            )
        if item.summary:
            score += 5
            matches.append(
                AdmissionRuleMatch("content.has_summary", "score", "summary", score_delta=5)
            )
        if item.published_at:
            score += 5
            matches.append(
                AdmissionRuleMatch(
                    "content.has_published_at", "score", "published_at", score_delta=5
                )
            )
        ai_match = _AI_RELEVANCE.search(title_and_summary)
        if ai_match:
            score += 15
            matches.append(
                AdmissionRuleMatch(
                    "content.ai_relevance", "score", "content", ai_match.group(0), 15
                )
            )
        detected_scopes: set[str] = set()
        for rule_id, pattern, delta in _POSITIVE_RULES:
            match = pattern.search(title_and_summary)
            if not match:
                continue
            score += delta
            matches.append(
                AdmissionRuleMatch(f"content.{rule_id}", "score", "content", match.group(0), delta)
            )
            detected_scopes.update(_rule_scopes(rule_id))
        for term in include_matches:
            score += 20
            matches.append(AdmissionRuleMatch("source.include_term", "score", "content", term, 20))

        if (
            config.content_scope
            and detected_scopes
            and detected_scopes.isdisjoint(config.content_scope)
        ):
            return _decision(
                False,
                "source.content_scope_mismatch",
                [
                    *matches,
                    AdmissionRuleMatch(
                        "source.content_scope_mismatch",
                        "reject",
                        "content_scope",
                        ",".join(sorted(detected_scopes)),
                    ),
                ],
                min(100, score),
            )
        if not item.summary and not config.accept_title_only:
            return _decision(
                False,
                "content.title_only_not_allowed",
                [
                    *matches,
                    AdmissionRuleMatch("content.title_only_not_allowed", "reject", "summary"),
                ],
                min(100, score),
            )
        if not item.summary and len(title) < 6 and not detected_scopes:
            return _decision(
                False,
                "content.insufficient_substance",
                [*matches, AdmissionRuleMatch("content.insufficient_substance", "reject", "title")],
                min(100, score),
            )

        score = max(0, min(100, score))
        if score < config.minimum_quality_score:
            return _decision(
                False,
                "quality.below_minimum",
                [
                    *matches,
                    AdmissionRuleMatch(
                        "quality.below_minimum",
                        "reject",
                        "minimum_quality_score",
                        str(config.minimum_quality_score),
                    ),
                ],
                score,
            )
        return _decision(
            True,
            "quality.threshold_met",
            [*matches, AdmissionRuleMatch("quality.threshold_met", "accept", "quality_score")],
            score,
        )


class _AdmissionConfig:
    def __init__(
        self,
        *,
        content_scope: frozenset[str],
        include_terms: tuple[str, ...],
        exclude_terms: tuple[str, ...],
        minimum_quality_score: int,
        accept_title_only: bool,
        allow_external_links: bool,
        allow_technical_updates: bool,
    ) -> None:
        self.content_scope = content_scope
        self.include_terms = include_terms
        self.exclude_terms = exclude_terms
        self.minimum_quality_score = minimum_quality_score
        self.accept_title_only = accept_title_only
        self.allow_external_links = allow_external_links
        self.allow_technical_updates = allow_technical_updates


def _validated_config(source: Source) -> _AdmissionConfig:
    scopes = _string_list(source.content_scope, "content_scope")
    allowed_scopes = {category.value for category in Category if category != Category.UNCLASSIFIED}
    unknown = set(scopes) - allowed_scopes
    if unknown:
        raise ValueError(f"unknown content_scope values: {', '.join(sorted(unknown))}")
    include_terms = _string_list(source.include_terms, "include_terms")
    exclude_terms = _string_list(source.exclude_terms, "exclude_terms")
    overlap = {value.casefold() for value in include_terms} & {
        value.casefold() for value in exclude_terms
    }
    if overlap:
        raise ValueError("include_terms and exclude_terms overlap")
    raw_allowed_primary_types = cast(object, source.allowed_primary_types)
    allowed_primary_types = _string_list(
        raw_allowed_primary_types if raw_allowed_primary_types is not None else [],
        "allowed_primary_types",
    )
    unknown_primary_types = set(allowed_primary_types) - {value.value for value in PrimaryType}
    if unknown_primary_types:
        raise ValueError(
            f"unknown allowed_primary_types values: {', '.join(sorted(unknown_primary_types))}"
        )
    score = source.minimum_quality_score
    if isinstance(score, bool) or not math.isfinite(score):
        raise ValueError("minimum_quality_score must be a finite number")
    if not 0 <= score <= 100:
        raise ValueError("minimum_quality_score must be between 0 and 100")
    for name in ("accept_title_only", "allow_external_links", "allow_technical_updates"):
        if not isinstance(getattr(source, name), bool):
            raise ValueError(f"{name} must be a boolean")
    return _AdmissionConfig(
        content_scope=frozenset(scopes),
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        minimum_quality_score=round(score),
        accept_title_only=source.accept_title_only,
        allow_external_links=source.allow_external_links,
        allow_technical_updates=source.allow_technical_updates,
    )


def _role_rejection(content: str, source_role: SourceRole) -> AdmissionResult | None:
    if source_role is SourceRole.OFFICIAL_PRODUCT and (
        match := _PRODUCT_DISALLOWED.search(content)
    ):
        return _hard_reject("role.official_product.non_product_activity", "content", match.group(0))
    if source_role is SourceRole.OFFICIAL_POLICY and (match := _POLICY_DISALLOWED.search(content)):
        return _hard_reject("role.official_policy.routine_activity", "content", match.group(0))
    if source_role is SourceRole.OFFICIAL_INDUSTRY and not _INDUSTRY_RELEVANCE.search(content):
        return _hard_reject("role.official_industry.ai_relevance_missing", "content")
    if (
        source_role is SourceRole.OPPORTUNITY_AND_AWARD_HUB
        and "通知" in content
        and not re.search(r"征集|申报|参评|报名|获奖|入选|名单|公示|政策|标准|报告", content)
    ):
        return _hard_reject("role.opportunity_hub.generic_notice", "content")
    return None


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    cleaned: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        term = item.strip()
        if len(term) > 100:
            raise ValueError(f"{name} entries must not exceed 100 characters")
        if term not in cleaned:
            cleaned.append(term)
    if len(cleaned) > 100:
        raise ValueError(f"{name} must not contain more than 100 entries")
    return tuple(cleaned)


def _github_rejection(content: str, allow_technical_updates: bool) -> AdmissionResult | None:
    prerelease = _GITHUB_PRERELEASE.search(content)
    if prerelease:
        return _hard_reject("github.prerelease", "content", prerelease.group(0))
    version = _SEMVER.search(content)
    if version and (int(version.group(2)) > 0 or int(version.group(3)) > 0):
        return _hard_reject("github.non_major_version", "title", version.group(0))
    maintenance = _GITHUB_MAINTENANCE.search(content)
    if maintenance:
        return _hard_reject("github.maintenance", "content", maintenance.group(0))
    if not allow_technical_updates:
        return _hard_reject("github.technical_updates_not_allowed", "source")
    if not version or int(version.group(1)) < 1:
        return _hard_reject("github.major_release_not_proven", "title")
    return None


def _rule_scopes(rule_id: str) -> Iterable[str]:
    return {
        "major_model_release": (Category.MODEL_TECHNOLOGY.value,),
        "agent_product_release": (Category.AGENT_PRODUCT.value,),
        "policy_or_standard": (Category.POLICY_INDUSTRY.value,),
        "solicitation_or_application": (Category.SOLICITATION.value,),
        "authoritative_list": (Category.AWARD_CASE.value,),
        "enterprise_outcome": (Category.ENTERPRISE_CASE.value,),
    }.get(rule_id, ())


def _valid_url(value: str) -> bool:
    split = urlsplit(value.strip())
    return split.scheme in {"http", "https"} and bool(split.hostname)


def _hard_reject(rule_id: str, field: str, value: str | None = None) -> AdmissionResult:
    return _decision(False, rule_id, [AdmissionRuleMatch(rule_id, "reject", field, value)], 0)


def _decision(
    accepted: bool,
    reason: str,
    matches: list[AdmissionRuleMatch],
    score: int,
) -> AdmissionResult:
    return AdmissionResult(accepted, reason, tuple(matches), max(0, min(100, score)))


# Backward-compatible import name.  The stage-eight-B architecture and pipeline use
# BasicAdmissionPolicy; older adapters and third-party extensions can migrate without
# a flag day.
ContentAdmissionPolicy = BasicAdmissionPolicy
