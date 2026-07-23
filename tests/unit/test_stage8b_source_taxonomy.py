# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
"""Focused stage-eight-B taxonomy, catalog, lifecycle and cleanup regression tests."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.collectors.registry import default_collector_registry
from app.domain.collection import CollectedItem
from app.domain.enums import (
    CaseCompleteness,
    CrawlStatus,
    ImplementationStatus,
    LifecycleState,
    PrimaryType,
    ReviewPolicy,
    ReviewStatus,
    RunTrigger,
    SourceOrigin,
    SourceRole,
    SourceType,
    VerificationStatus,
)
from app.domain.models import (
    CrawlRun,
    CrawlSourceExecution,
    IntelligenceItem,
    ItemReviewEvent,
    ItemRevision,
    Source,
)
from app.domain.taxonomy import OpportunityFields, TaxonomyResult, VerificationResult
from app.domain.update import SourcePreviewResult, SourceUpdateStatus
from app.services.publication_policy import PublicationPolicy
from app.services.retired_source_purge import RetiredSourcePurgeService
from app.services.source_catalog_service import SourceCatalogService, load_source_catalog
from app.services.source_lifecycle_service import SourceActivationError, SourceLifecycleService
from app.services.taxonomy_classification import TaxonomyClassificationService
from app.services.verification_service import VerificationService
from app.storage.database import Database
from app.storage.repositories import RepositoryUnitOfWork
from app.web.app import create_app


def _source(role: SourceRole, **changes: object) -> Source:
    values: dict[str, object] = {
        "slug": f"source-{role.value}",
        "name": role.value,
        "source_type": SourceType.HTML_LIST,
        "start_url": f"https://{role.value}.example/list",
        "collector_name": "html_list",
        "collector_config": {},
        "origin": SourceOrigin.PRESET,
        "lifecycle_state": LifecycleState.ACTIVE,
        "enabled": True,
        "source_role": role,
        "review_policy": ReviewPolicy.AUTO_PUBLISH,
        "homepage_visible": True,
        "export_visible": True,
        "allowed_primary_types": [],
    }
    values.update(changes)
    return Source(**values)  # pyright: ignore[reportArgumentType]


def _item(
    title: str,
    summary: str = "正式公开内容",
    *,
    extra: dict[str, object] | None = None,
) -> CollectedItem:
    return CollectedItem(
        title=title,
        original_url="https://official.example/item/1",
        canonical_url="https://official.example/item/1",
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        summary=summary,
        extra=extra or {},
    )


@pytest.mark.parametrize(
    ("role", "title", "expected"),
    [
        (SourceRole.OFFICIAL_PRODUCT, "新一代大模型正式发布并开源", PrimaryType.PRODUCT_UPDATE),
        (SourceRole.OFFICIAL_PRODUCT, "Agent 平台重大升级", PrimaryType.PRODUCT_UPDATE),
        (SourceRole.OFFICIAL_POLICY, "人工智能管理办法正式发布", PrimaryType.POLICY_STANDARD),
        (
            SourceRole.OPPORTUNITY_AND_AWARD_HUB,
            "关于征集人工智能案例的通知",
            PrimaryType.APPLICATION_OPPORTUNITY,
        ),
        (
            SourceRole.OPPORTUNITY_AND_AWARD_HUB,
            "人工智能典型案例入选名单公布",
            PrimaryType.AWARD_RESULT,
        ),
        (SourceRole.REPORT_HUB, "人工智能白皮书正式发布", PrimaryType.REPORT_RELEASE),
        (SourceRole.MEDIA_DISCOVERY, "某企业发布人工智能产品线索", PrimaryType.INDUSTRY_SIGNAL),
        (SourceRole.FALLBACK, "信息含义不明确", PrimaryType.UNCLASSIFIED),
    ],
)
def test_taxonomy_primary_type_is_single_and_deterministic(
    role: SourceRole, title: str, expected: PrimaryType
) -> None:
    result = TaxonomyClassificationService().classify(_item(title), role)

    assert result.primary_type is expected
    assert result.taxonomy_version == "v2"
    assert len(set(result.topic_tags)) == len(result.topic_tags)


def test_taxonomy_tags_opportunity_and_case_completeness_are_orthogonal() -> None:
    classifier = TaxonomyClassificationService()
    product = classifier.classify(
        _item("Agent 平台大模型 API 正式发布并开源"), SourceRole.OFFICIAL_PRODUCT
    )
    opportunity = classifier.classify(
        _item(
            "关于征集人工智能案例的通知",
            "主办机构: 中国协会; 申报对象: 制造企业; 截止日期: 2026年8月1日",
        ),
        SourceRole.OPPORTUNITY_AND_AWARD_HUB,
    )
    full_case = classifier.classify(
        _item(
            "制造企业人工智能案例落地取得成效",
            extra={
                "detail_fetched": True,
                "content": (
                    "业务背景存在生产痛点。建设大模型技术方案并完成部署实施, "
                    "应用结果使效率提升35%, 成本降低20%。"
                ),
            },
        ),
        SourceRole.OFFICIAL_CASE_HUB,
    )
    lead = classifier.classify(_item("人工智能典型案例入选名单公布"), SourceRole.OFFICIAL_CASE_HUB)

    assert [tag.value for tag in product.topic_tags] == [
        "model",
        "agent",
        "agent_platform",
        "api",
        "open_source",
    ]
    assert opportunity.primary_type is PrimaryType.APPLICATION_OPPORTUNITY
    assert opportunity.opportunity.deadline_at is not None
    assert opportunity.opportunity.deadline_at.date().isoformat() == "2026-08-01"
    assert full_case.primary_type is PrimaryType.CASE_ANALYSIS
    assert full_case.case_completeness is CaseCompleteness.FULL_CASE
    assert lead.primary_type is PrimaryType.AWARD_RESULT
    assert lead.case_completeness is CaseCompleteness.CASE_LEAD


def test_media_verification_and_publication_are_separate() -> None:
    source = _source(
        SourceRole.MEDIA_DISCOVERY,
        review_policy=ReviewPolicy.ALWAYS_REVIEW,
        homepage_visible=False,
        export_visible=False,
    )
    item = _item("消息称某企业或将发布大模型")
    taxonomy = TaxonomyClassificationService().classify(item, source.source_role)
    verification = VerificationService().verify(item, source)
    decision = PublicationPolicy().decide(
        source=source,
        admission_accepted=True,
        taxonomy=taxonomy,
        verification=verification,
    )

    assert verification.verification_status is VerificationStatus.RUMOR_OR_PREDICTION
    assert verification.review_status is ReviewStatus.PENDING
    assert decision.industry_leads is True
    assert decision.review_queue is True
    assert decision.leadership_homepage is False
    assert decision.formal_export is False


def test_official_linked_and_approved_media_can_be_published_when_explicitly_visible() -> None:
    source = _source(SourceRole.MEDIA_DISCOVERY)
    taxonomy = TaxonomyResult(
        PrimaryType.PRODUCT_UPDATE,
        (),
        (),
        CaseCompleteness.NOT_CASE,
        OpportunityFields(),
        "v2",
        ("manual.reclassified",),
        "manual official link verification",
        1.0,
    )
    verification = VerificationResult(
        VerificationStatus.OFFICIAL_LINKED,
        ReviewStatus.APPROVED,
        "https://media.example/story",
        "https://official.example/release",
        "Official publisher",
    )

    decision = PublicationPolicy().decide(
        source=source,
        admission_accepted=True,
        taxonomy=taxonomy,
        verification=verification,
    )

    assert decision.leadership_homepage is True
    assert decision.formal_export is True


def test_catalog_sync_imports_every_entry_is_idempotent_and_preserves_paused(
    database: Database,
) -> None:
    service = SourceCatalogService(lambda: RepositoryUnitOfWork(database))
    catalog = load_source_catalog()

    first = service.sync()
    second = service.sync()
    with RepositoryUnitOfWork(database) as uow:
        sources = uow.sources.list()
        paused = uow.sources.get_by_slug("nda-news")
        assert paused is not None
        paused.lifecycle_state = LifecycleState.PAUSED
        paused.enabled = False
    third = service.sync()

    assert first.total == len(catalog) == 26
    assert (first.created, first.active, first.candidate) == (26, 18, 8)
    assert (second.existing, second.conflicts) == (26, 0)
    assert len(sources) == 26
    assert all(
        entry.implementation_reason
        for entry in catalog
        if entry.lifecycle_state is LifecycleState.CANDIDATE
    )
    assert (third.paused, third.conflicts) == (1, 0)
    with RepositoryUnitOfWork(database) as uow:
        assert uow.sources.get_by_slug("nda-news").lifecycle_state is LifecycleState.PAUSED  # type: ignore[union-attr]
        assert not any(
            source.slug
            in {
                "openai-news-rss",
                "google-blog-rss",
                "qwen-agent-releases",
                "baidu-cloud-customer-cases",
            }
            for source in uow.sources.list()
        )


def test_source_management_page_lists_active_and_candidate_catalog_rows(
    database: Database,
) -> None:
    entries = load_source_catalog()
    SourceCatalogService(lambda: RepositoryUnitOfWork(database)).sync()
    with RepositoryUnitOfWork(database) as uow:
        media = uow.sources.get_by_slug("cls-ai-subject")
        assert media is not None
        media.lifecycle_state = LifecycleState.ACTIVE
        media.enabled = True

    with TestClient(create_app(database=database, enforce_migrations=False)) as client:
        response = client.get("/sources", params={"per_page": 100})
        candidate_response = client.get(
            "/sources", params={"tab": "candidate", "per_page": 100}
        )

    assert response.status_code == 200
    assert candidate_response.status_code == 200
    combined = response.text + candidate_response.text
    for entry in entries:
        assert entry.slug in combined
    assert "正常" in response.text
    assert "候选" in candidate_response.text
    assert "媒体发现" in response.text


def test_candidate_activation_is_preview_gated(database: Database) -> None:
    candidate = _source(
        SourceRole.OFFICIAL_PRODUCT,
        slug="candidate-source",
        lifecycle_state=LifecycleState.CANDIDATE,
        enabled=False,
        implementation_status=ImplementationStatus.RESEARCH_NEEDED,
    )
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(candidate)
    lifecycle = SourceLifecycleService(
        lambda: RepositoryUnitOfWork(database), default_collector_registry().names()
    )
    preview = SourcePreviewResult(
        candidate.id,
        candidate.name,
        SourceUpdateStatus.SUCCESS,
        fetched=1,
        normalized=1,
        accepted=1,
        valid_title_ratio=1.0,
        valid_link_ratio=1.0,
        valid_date_ratio=1.0,
    )

    with pytest.raises(SourceActivationError, match="explicit"):
        lifecycle.activate("candidate-source", preview, confirm=False)
    lifecycle.activate("candidate-source", preview, confirm=True)

    with RepositoryUnitOfWork(database) as uow:
        active = uow.sources.get_by_slug("candidate-source")
        assert active is not None
        assert active.lifecycle_state is LifecycleState.ACTIVE
        assert active.enabled is True
        assert active.preview_item_count == 1


def test_candidate_activation_rejects_missing_dates_and_excessive_duplicates(
    database: Database,
) -> None:
    candidates = (
        _source(
            SourceRole.OFFICIAL_PRODUCT,
            slug="candidate-missing-dates",
            start_url="https://candidate-missing-dates.example/list",
            lifecycle_state=LifecycleState.CANDIDATE,
            enabled=False,
            implementation_status=ImplementationStatus.RESEARCH_NEEDED,
        ),
        _source(
            SourceRole.OFFICIAL_PRODUCT,
            slug="candidate-duplicates",
            start_url="https://candidate-duplicates.example/list",
            lifecycle_state=LifecycleState.CANDIDATE,
            enabled=False,
            implementation_status=ImplementationStatus.RESEARCH_NEEDED,
        ),
    )
    with RepositoryUnitOfWork(database) as uow:
        for candidate in candidates:
            uow.sources.add(candidate)
    lifecycle = SourceLifecycleService(
        lambda: RepositoryUnitOfWork(database), default_collector_registry().names()
    )

    missing_dates = SourcePreviewResult(
        candidates[0].id,
        candidates[0].name,
        SourceUpdateStatus.SUCCESS,
        fetched=10,
        normalized=10,
        accepted=10,
        valid_title_ratio=1.0,
        valid_date_ratio=0.6,
        valid_link_ratio=1.0,
    )
    duplicates = SourcePreviewResult(
        candidates[1].id,
        candidates[1].name,
        SourceUpdateStatus.SUCCESS,
        fetched=10,
        normalized=10,
        accepted=10,
        valid_title_ratio=1.0,
        valid_date_ratio=1.0,
        valid_link_ratio=1.0,
        duplicate_count=3,
        duplicate_ratio=0.3,
    )

    with pytest.raises(SourceActivationError, match="date validity"):
        lifecycle.activate("candidate-missing-dates", missing_dates, confirm=True)
    with pytest.raises(SourceActivationError, match="duplicate ratio"):
        lifecycle.activate("candidate-duplicates", duplicates, confirm=True)
    with RepositoryUnitOfWork(database) as uow:
        assert (
            uow.sources.get_by_slug("candidate-missing-dates").lifecycle_state  # type: ignore[union-attr]
            is LifecycleState.CANDIDATE
        )
        assert (
            uow.sources.get_by_slug("candidate-duplicates").lifecycle_state  # type: ignore[union-attr]
            is LifecycleState.CANDIDATE
        )


def test_bulk_source_selection_includes_active_media_but_excludes_candidate_and_paused(
    database: Database,
) -> None:
    SourceCatalogService(lambda: RepositoryUnitOfWork(database)).sync()
    with RepositoryUnitOfWork(database) as uow:
        media = uow.sources.get_by_slug("cls-ai-subject")
        paused = uow.sources.get_by_slug("nda-news")
        assert media is not None and paused is not None
        media.lifecycle_state = LifecycleState.ACTIVE
        media.enabled = True
        paused.lifecycle_state = LifecycleState.PAUSED
        paused.enabled = False
    with RepositoryUnitOfWork(database) as uow:
        selected = {source.slug for source in uow.sources.list_active()}

    assert "cls-ai-subject" in selected
    assert "nda-news" not in selected
    assert "qwen-official-blog" in selected


def test_report_parent_child_repository_query_and_unique_fingerprints(database: Database) -> None:
    source = _source(SourceRole.REPORT_HUB)
    with RepositoryUnitOfWork(database) as uow:
        uow.sources.add(source)
        parent = uow.items.add(
            IntelligenceItem(
                source_id=source.id,
                title="人工智能行业报告",
                original_url="https://report.example/report",
                canonical_url="https://report.example/report",
                fingerprint="a" * 64,
                primary_type=PrimaryType.REPORT_RELEASE,
                taxonomy_version="v2",
            )
        )
        for index in range(2):
            uow.items.add(
                IntelligenceItem(
                    source_id=source.id,
                    parent_item_id=parent.id,
                    title=f"报告案例 {index + 1}",
                    original_url=f"https://report.example/report#case-{index + 1}",
                    canonical_url=f"https://report.example/report#case-{index + 1}",
                    fingerprint=str(index + 1) * 64,
                    primary_type=PrimaryType.CASE_ANALYSIS,
                    taxonomy_version="v2",
                )
            )
    with RepositoryUnitOfWork(database) as uow:
        children = uow.items.list_children(parent.id)

    assert [item.title for item in children] == ["报告案例 1", "报告案例 2"]
    assert len({item.fingerprint for item in children}) == 2


def test_retired_purge_dry_run_backup_confirm_and_mixed_run_survival(tmp_path: Path) -> None:
    database_path = tmp_path / "copy.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_schema()
    try:
        retired = _source(
            SourceRole.OFFICIAL_PRODUCT,
            slug="openai-news-rss",
            start_url="https://openai.com/news/rss.xml",
        )
        kept = _source(
            SourceRole.OFFICIAL_PRODUCT,
            slug="baidu-cloud-news",
            start_url="https://cloud.baidu.com/news/news",
        )
        with RepositoryUnitOfWork(database) as uow:
            uow.sources.add(retired)
            uow.sources.add(kept)
            item = uow.items.add(
                IntelligenceItem(
                    source_id=retired.id,
                    title="retired",
                    original_url="https://openai.com/news/old",
                    canonical_url="https://openai.com/news/old",
                    fingerprint="f" * 64,
                    is_favorite=True,
                )
            )
            run = uow.crawl_runs.add(
                CrawlRun(status=CrawlStatus.SUCCESS, trigger=RunTrigger.MANUAL_CLI)
            )
            uow.crawl_source_executions.add(
                CrawlSourceExecution(crawl_run_id=run.id, source_id=retired.id, status="success")
            )
            uow.crawl_source_executions.add(
                CrawlSourceExecution(crawl_run_id=run.id, source_id=kept.id, status="success")
            )
            uow.revisions.add(
                ItemRevision(item_id=item.id, crawl_run_id=run.id, old_data={}, new_data={})
            )
            uow.review_events.add(
                ItemReviewEvent(item_id=item.id, actor_source="test", old_data={}, new_data={})
            )

        purge = RetiredSourcePurgeService(database, lambda: RepositoryUnitOfWork(database))
        dry_run = purge.plan()
        assert dry_run.dry_run is True
        assert [
            (impact.slug, impact.item_count, impact.crawl_execution_count)
            for impact in dry_run.impacts
        ] == [("openai-news-rss", 1, 1)]
        with RepositoryUnitOfWork(database) as uow:
            assert len(uow.sources.list()) == 2

        backup = tmp_path / "before-purge.db"
        confirmed = purge.purge(confirm=True, backup_path=backup)
        assert backup.is_file()
        assert (
            confirmed.deleted_sources,
            confirmed.deleted_items,
            confirmed.deleted_executions,
        ) == (1, 1, 1)
        with RepositoryUnitOfWork(database) as uow:
            assert [source.slug for source in uow.sources.list()] == ["baidu-cloud-news"]
            assert len(uow.crawl_runs.list()) == 1
            assert uow.crawl_source_executions.count_by_source(kept.id) == 1
            assert uow.revisions.list() == []
            assert uow.review_events.list() == []
        assert purge.plan().impacts == ()
    finally:
        database.dispose()


def test_new_domestic_media_sources_exist_in_catalog() -> None:
    catalog = load_source_catalog()
    slugs = {e.slug for e in catalog}

    for expected in ("leiphone-ai", "geekpark-ai", "ithome-ai", "huxiu-ai"):
        assert expected in slugs, f"missing catalog entry: {expected}"


def test_domestic_media_sources_have_correct_unified_settings() -> None:
    catalog = load_source_catalog()
    domestic = [
        e
        for e in catalog
        if e.slug in {"leiphone-ai", "geekpark-ai", "ithome-ai", "huxiu-ai"}
    ]

    for entry in domestic:
        assert entry.source_role.value == "media_discovery", (
            f"{entry.slug}: source_role should be media_discovery"
        )
        assert entry.review_policy.value == "always_review", (
            f"{entry.slug}: review_policy should be always_review"
        )
        if entry.slug == "ithome-ai":
            assert entry.lifecycle_state.value == "active", (
                f"{entry.slug}: lifecycle should be active"
            )
        else:
            assert entry.lifecycle_state.value == "candidate", (
                f"{entry.slug}: lifecycle should be candidate"
            )
        assert entry.max_items_per_run is not None
        assert entry.max_items_per_run >= 1


def test_leiphone_ai_has_category_filter_config() -> None:
    catalog = load_source_catalog()
    entry = next(e for e in catalog if e.slug == "leiphone-ai")

    assert entry.collector_name == "rss"
    config = entry.collector_config
    assert "include_categories" in config
    categories: object = config["include_categories"]
    assert "人工智能" in categories  # pyright: ignore[reportOperatorIssue]


def test_geekpark_is_blocked_with_notes() -> None:
    catalog = load_source_catalog()
    entry = next(e for e in catalog if e.slug == "geekpark-ai")

    assert entry.implementation_status.value == "blocked_by_javascript"
    assert entry.collector_name == "custom"
    assert entry.collector_config == {}
    assert "WAF" in entry.notes or "WAF" in (entry.implementation_reason or "")


def test_huxiu_ai_has_slug_and_notes() -> None:
    catalog = load_source_catalog()
    entry = next(e for e in catalog if e.slug == "huxiu-ai")

    assert entry.collector_name == "huxiu"
    assert entry.lifecycle_state.value == "candidate"
    assert entry.implementation_reason is not None
    assert len(entry.implementation_reason) > 10


def test_all_domestic_media_include_terms_present() -> None:
    catalog = load_source_catalog()
    domestic = [
        e
        for e in catalog
        if e.slug in {"leiphone-ai", "geekpark-ai", "ithome-ai", "huxiu-ai"}
    ]

    for entry in domestic:
        assert entry.include_terms, f"{entry.slug}: include_terms should not be empty"
        terms = entry.include_terms
        assert "人工智能" in terms or "AI" in terms, f"{entry.slug}: missing AI terms"
