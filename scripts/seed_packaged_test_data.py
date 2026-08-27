#!/usr/bin/env python3
"""Seed the isolated database used by the Windows packaged integration test."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from app.domain.enums import (
    Category,
    CrawlMode,
    DiscoveryStatus,
    ImplementationStatus,
    LifecycleState,
    PrimaryType,
    ReviewPolicy,
    ReviewStatus,
    SourceAudience,
    SourceKind,
    SourceOrigin,
    SourceRole,
    SourceTier,
    SourceType,
    VerificationStatus,
)
from app.domain.models import (
    AIProviderSetting,
    AISettings,
    IntelligenceItem,
    ScheduleSettings,
    Source,
)
from app.storage.database import Database

MARKER = "打包集成持久化标记-PACKAGED_EXPORT_MARKER"
SOURCE_SLUG = "packaged-integration-fixture"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--fixture-base-url", required=True)
    args = parser.parse_args()
    base_url = _loopback_fixture_url(args.fixture_base_url)
    database_path = args.database.resolve()
    if not database_path.is_file():
        parser.error("packaged application must initialize the database before seeding")

    database = Database(f"sqlite:///{database_path.as_posix()}")
    now = datetime.now(UTC)
    try:
        with database.session() as session:
            source = session.query(Source).filter(Source.slug == SOURCE_SLUG).one_or_none()
            if source is None:
                source = Source(slug=SOURCE_SLUG, name="Windows 打包集成正式来源", start_url="")
                session.add(source)
            source.name = "Windows 打包集成正式来源"
            source.description = "仅由临时本机 HTTP fixture server 提供的确定性测试来源"
            source.source_type = SourceType.RSS
            source.start_url = f"{base_url}/feed.xml"
            source.enabled = True
            source.lifecycle_state = LifecycleState.ACTIVE
            source.source_role = SourceRole.OFFICIAL_PRODUCT
            source.crawl_mode = CrawlMode.RSS
            source.review_policy = ReviewPolicy.AUTO_PUBLISH
            source.allowed_primary_types = [PrimaryType.PRODUCT_UPDATE.value]
            source.lookback_days = 30
            source.max_items_per_run = 20
            source.implementation_status = ImplementationStatus.READY
            source.implementation_reason = "deterministic packaged integration fixture"
            source.activation_evidence = "AIM_DESKTOP_TEST_MODE=1"
            source.verified_at = now
            source.collector_name = "rss"
            source.collector_config = {}
            source.discovery_status = DiscoveryStatus.READY.value
            source.discovery_confidence = 1.0
            source.requires_custom_collector = False
            source.origin = SourceOrigin.PRESET
            source.source_kind = SourceKind.FORMAL
            source.source_tier = SourceTier.OFFICIAL_COMPANY
            source.audience = SourceAudience.ALL
            source.homepage_visible = True
            source.export_visible = True
            source.content_scope = []
            source.include_terms = []
            source.exclude_terms = []
            source.minimum_quality_score = 0
            source.accept_title_only = True
            source.allow_external_links = False
            source.allow_technical_updates = True
            session.flush()

            marker_url = f"{base_url}/items/persisted-marker"
            marker = (
                session.query(IntelligenceItem)
                .filter(IntelligenceItem.canonical_url == marker_url)
                .one_or_none()
            )
            if marker is None:
                session.add(
                    IntelligenceItem(
                        source_id=source.id,
                        title=MARKER,
                        original_url=marker_url,
                        canonical_url=marker_url,
                        summary="用于验证 Excel、Word 与重启后 SQLite 持久化的中文标记。",
                        discovered_at=now,
                        category=Category.AGENT_PRODUCT,
                        primary_type=PrimaryType.PRODUCT_UPDATE,
                        verification_status=VerificationStatus.OFFICIAL_CONFIRMED,
                        review_status=ReviewStatus.NOT_REQUIRED,
                        fingerprint="packaged-export-marker-v2",
                        admission_accepted=True,
                    )
                )

            schedule = session.get(ScheduleSettings, 1)
            if schedule is None:
                schedule = ScheduleSettings(id=1, timezone="UTC")
                session.add(schedule)
            schedule.schedule_enabled = True
            schedule.schedule_hour = 0
            schedule.schedule_minute = 0
            schedule.schedule_days_mask = 127
            schedule.initial_fetch_days = 30
            schedule.timezone = "UTC"
            schedule.updated_at = now
            schedule.last_scheduled_trigger_at = None

            workflow = session.get(AISettings, 1)
            if workflow is None:
                workflow = AISettings(id=1)
                session.add(workflow)
            workflow.selected_provider = "custom"
            workflow.classifier_mode = "off"
            workflow.summarizer_mode = "auto"
            workflow.classification_provider = "custom"
            workflow.classification_model = "packaged-zero-token-fixture"
            workflow.summarization_provider = "custom"
            workflow.summarization_model = "packaged-zero-token-fixture"
            workflow.classification_batch_mode = "quick"
            workflow.api_key = ""
            workflow.updated_at = now

            provider = session.get(AIProviderSetting, "custom")
            if provider is None:
                provider = AIProviderSetting(
                    provider="custom",
                    base_url=base_url,
                    model="packaged-zero-token-fixture",
                )
                session.add(provider)
            provider.base_url = base_url
            provider.model = "packaged-zero-token-fixture"
            provider.api_key = "packaged-test-key-no-external-service"
            provider.timeout_seconds = 5
            provider.max_retries = 0
            provider.enabled = True
            provider.updated_at = now
            session.commit()
        return 0
    finally:
        database.dispose()


def _loopback_fixture_url(value: str) -> str:
    normalized = value.rstrip("/")
    parts = urlsplit(normalized)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("fixture base URL must be an explicit loopback HTTP endpoint")
    if parts.path or parts.query or parts.fragment or parts.port is None:
        raise ValueError("fixture base URL must contain only host and port")
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
