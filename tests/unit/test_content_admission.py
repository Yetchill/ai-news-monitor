"""Stage-eight content-admission and formal-source behavior without public network."""

from datetime import UTC, datetime

import pytest

from app.domain.collection import CollectedItem
from app.domain.enums import (
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceScope,
    SourceTier,
    SourceType,
)
from app.domain.models import IntelligenceItem, Source
from app.domain.queries import ItemFilter, ItemQuery
from app.services.content_admission import ContentAdmissionPolicy
from app.services.web_data_service import WebDataService
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork


def _source(**changes: object) -> Source:
    values: dict[str, object] = {
        "name": "正式来源",
        "source_type": SourceType.RSS,
        "start_url": "https://official.example/feed",
        "enabled": True,
        "collector_name": "rss",
        "collector_config": {},
        "origin": SourceOrigin.PRESET,
        "source_kind": SourceKind.FORMAL,
        "source_tier": SourceTier.GOVERNMENT,
        "audience": SourceAudience.LEADERSHIP,
        "homepage_visible": True,
        "export_visible": True,
        "content_scope": [],
        "include_terms": [],
        "exclude_terms": [],
        "minimum_quality_score": 50,
        "accept_title_only": True,
        "allow_external_links": False,
        "allow_technical_updates": False,
    }
    values.update(changes)
    return Source(**values)  # pyright: ignore[reportArgumentType]


def _item(title: str, *, summary: str | None = "正式发布内容与业务结果") -> CollectedItem:
    return CollectedItem(
        title=title,
        original_url="https://official.example/news/1",
        canonical_url="https://official.example/news/1",
        summary=summary,
        published_at=datetime(2026, 7, 19, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("人工智能企业校园招聘公告", "content.recruitment"),
        ("大模型工程师培训班招生", "content.training_promotion"),
        ("人工智能大会活动回顾", "content.event_recap"),
        ("人工智能联盟会员服务介绍", "content.membership_promotion"),
        ("AI 产品限时优惠活动", "content.commercial_promotion"),
        ("人工智能大会会议日程", "content.event_preview"),
        ("登录", "content.login_page"),
        ("联系我们", "content.contact_page"),
    ],
)
def test_default_low_value_content_is_rejected(title: str, reason: str) -> None:
    result = ContentAdmissionPolicy().admit(_item(title), _source())

    assert result.accepted is False
    assert result.reason == reason
    assert result.quality_score == 0
    assert result.matched_rules[0].rule_id == reason
    assert result.matched_rules[0].effect == "reject"


@pytest.mark.parametrize(
    "title",
    [
        "新一代大模型正式发布",
        "企业级智能体平台正式发布",
        "关于推进人工智能发展的政策通知",
        "关于开展人工智能成果征集的通知",
        "人工智能示范项目申报通知",
        "人工智能优秀案例入选名单公布",
        "人工智能在制造企业落地取得明确业务成果",
    ],
)
def test_formal_high_value_content_is_accepted(title: str) -> None:
    result = ContentAdmissionPolicy().admit(_item(title), _source())

    assert result.accepted is True
    assert result.reason == "quality.threshold_met"
    assert 50 <= result.quality_score <= 100
    assert any(match.effect == "score" for match in result.matched_rules)
    assert result.matched_rules[-1].effect == "accept"


def test_source_include_exclude_and_exclude_priority_are_auditable() -> None:
    policy = ContentAdmissionPolicy()
    source = _source(include_terms=["智能体"], exclude_terms=["促销"])

    missing = policy.admit(_item("人工智能政策通知"), source)
    included = policy.admit(_item("智能体平台正式发布"), source)
    excluded = policy.admit(_item("智能体平台促销发布"), source)

    assert missing.reason == "source.include_term_missing"
    assert included.accepted is True
    assert any(match.rule_id == "source.include_term" for match in included.matched_rules)
    assert excluded.reason == "source.exclude_term"


def test_invalid_source_configuration_fails_closed() -> None:
    source = _source(include_terms="人工智能", minimum_quality_score=101)

    result = ContentAdmissionPolicy().admit(_item("新大模型正式发布"), source)

    assert result.accepted is False
    assert result.reason == "source.configuration_invalid"
    assert result.quality_score == 0


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("v2.0.1 patch release", "github.non_major_version"),
        ("v2.0.0 bug fix", "github.maintenance"),
        ("v2.0.0 dependency update", "github.maintenance"),
        ("v2.0.0-rc1 major release", "github.prerelease"),
    ],
)
def test_github_maintenance_releases_are_rejected(title: str, reason: str) -> None:
    source = _source(
        source_type=SourceType.GITHUB_RELEASE,
        source_kind=SourceKind.FALLBACK,
        source_tier=SourceTier.FALLBACK,
        allow_technical_updates=True,
        minimum_quality_score=15,
    )

    result = ContentAdmissionPolicy().admit(_item(title), source)

    assert result.accepted is False
    assert result.reason == reason


def test_manually_allowed_github_major_release_can_pass() -> None:
    source = _source(
        source_type=SourceType.GITHUB_RELEASE,
        source_kind=SourceKind.FALLBACK,
        source_tier=SourceTier.FALLBACK,
        allow_technical_updates=True,
        minimum_quality_score=15,
    )

    result = ContentAdmissionPolicy().admit(_item("v2.0.0 major Agent platform release"), source)

    assert result.accepted is True


def test_default_home_hides_nonformal_and_explicit_all_shows_history(database: Database) -> None:
    formal = _source()
    test_source = _source(
        name="测试来源",
        start_url="https://test.example/feed",
        source_kind=SourceKind.TEST,
        source_tier=SourceTier.FALLBACK,
        homepage_visible=False,
        export_visible=False,
        enabled=False,
    )
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(formal)
        uow.sources.add(test_source)
        for index, source in enumerate((formal, test_source)):
            uow.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    title=f"资讯 {index}",
                    original_url=f"https://items.example/{index}",
                    canonical_url=f"https://items.example/{index}",
                    fingerprint=f"{index:064x}",
                )
            )
        uow.items.add(
            IntelligenceItem(
                source_id=formal.id,
                title="阶段七历史资讯",
                original_url="https://items.example/historical",
                canonical_url="https://items.example/historical",
                fingerprint="f" * 64,
                admission_accepted=False,
            )
        )

    service = WebDataService(lambda: RepositoryUnitOfWork(database))
    leadership = service.list_items(ItemQuery())
    all_sources = service.list_items(ItemQuery(source_scope=SourceScope.ALL))
    non_formal = service.list_items(ItemQuery(source_scope=SourceScope.NON_FORMAL))
    disabled = service.list_items(ItemQuery(source_scope=SourceScope.DISABLED))

    assert [entry.source_name for entry in leadership.entries] == ["正式来源"]
    assert {entry.source_name for entry in all_sources.entries} == {"正式来源", "测试来源"}
    assert {entry.title for entry in all_sources.entries} == {"资讯 0", "资讯 1", "阶段七历史资讯"}
    assert [entry.source_name for entry in non_formal.entries] == ["测试来源"]
    assert [entry.source_name for entry in disabled.entries] == ["测试来源"]
    with RepositoryUnitOfWork(database) as uow:
        assert uow.items.count_filtered(ItemFilter(source_scope=SourceScope.FORMAL_EXPORT)) == 1
