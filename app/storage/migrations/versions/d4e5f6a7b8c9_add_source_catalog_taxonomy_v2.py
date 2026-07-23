"""add source catalog lifecycle and taxonomy v2

Revision ID: d4e5f6a7b8c9
Revises: b6f4e2d9a731
Create Date: 2026-07-20 10:00:00.000000

The migration is deliberately structural and conservative.  It never deletes a
business source or item.  Retired-source deletion is performed by the separate,
backup-gated ``sources purge-retired`` operation.  Downgrade removes taxonomy-v2
and catalog metadata, so operators must export any manual v2 review audit they
need before downgrading; legacy category, manual_category, favorites and content
remain intact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "b6f4e2d9a731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.add_column(sa.Column("slug", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                _enum("lifecyclestate", "candidate", "active", "paused"),
                nullable=False,
                server_default="candidate",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_role",
                _enum(
                    "sourcerole",
                    "official_product",
                    "official_policy",
                    "official_industry",
                    "opportunity_and_award_hub",
                    "official_case_hub",
                    "report_hub",
                    "media_discovery",
                    "fallback",
                ),
                nullable=False,
                server_default="fallback",
            )
        )
        batch_op.add_column(
            sa.Column(
                "crawl_mode",
                _enum(
                    "crawlmode",
                    "rss",
                    "html_list",
                    "single_page_changelog",
                    "document_hub",
                    "case_hub",
                    "api",
                    "custom",
                    "rsshub",
                ),
                nullable=False,
                server_default="custom",
            )
        )
        batch_op.add_column(
            sa.Column(
                "review_policy",
                _enum(
                    "reviewpolicy",
                    "auto_publish",
                    "review_on_low_confidence",
                    "always_review",
                    "never_publish",
                ),
                nullable=False,
                server_default="always_review",
            )
        )
        batch_op.add_column(
            sa.Column("allowed_primary_types", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("lookback_days", sa.Integer(), nullable=False, server_default="30")
        )
        batch_op.add_column(
            sa.Column("max_items_per_run", sa.Integer(), nullable=False, server_default="20")
        )
        batch_op.add_column(
            sa.Column(
                "implementation_status",
                _enum(
                    "implementationstatus",
                    "ready",
                    "needs_custom_collector",
                    "blocked_by_javascript",
                    "research_needed",
                ),
                nullable=False,
                server_default="research_needed",
            )
        )
        batch_op.add_column(sa.Column("implementation_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("activation_evidence", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_preview_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("preview_item_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("preview_result", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("catalog_managed", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("catalog_fingerprint", sa.String(length=64), nullable=True))

    op.execute("UPDATE sources SET slug = 'legacy-source-' || id")
    op.execute(
        "UPDATE sources SET lifecycle_state = CASE WHEN enabled = 1 THEN 'active' ELSE 'paused' END"
    )
    op.execute(
        "UPDATE sources SET crawl_mode = CASE source_type "
        "WHEN 'rss' THEN 'rss' WHEN 'html_list' THEN 'html_list' "
        "WHEN 'json_api' THEN 'api' ELSE 'custom' END"
    )
    op.execute(
        "UPDATE sources SET implementation_status = CASE "
        "WHEN requires_custom_collector = 1 THEN 'needs_custom_collector' ELSE 'ready' END"
    )
    with op.batch_alter_table("sources") as batch_op:
        batch_op.create_unique_constraint("uq_sources_slug", ["slug"])

    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "primary_type",
                _enum(
                    "primarytype",
                    "unclassified",
                    "product_update",
                    "policy_standard",
                    "application_opportunity",
                    "award_result",
                    "report_release",
                    "case_analysis",
                    "industry_signal",
                ),
                nullable=False,
                server_default="unclassified",
            )
        )
        batch_op.add_column(
            sa.Column(
                "manual_primary_type",
                _enum(
                    "manualprimarytype",
                    "unclassified",
                    "product_update",
                    "policy_standard",
                    "application_opportunity",
                    "award_result",
                    "report_release",
                    "case_analysis",
                    "industry_signal",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("topic_tags", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("manual_topic_tags", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("industry_tags", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("manual_industry_tags", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "verification_status",
                _enum(
                    "verificationstatus",
                    "official_confirmed",
                    "official_linked",
                    "multi_source_confirmed",
                    "media_only",
                    "rumor_or_prediction",
                ),
                nullable=False,
                server_default="media_only",
            )
        )
        batch_op.add_column(
            sa.Column(
                "review_status",
                _enum("reviewstatus", "not_required", "pending", "approved", "rejected"),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column(
                "verification_manually_set", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "review_manually_set", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "case_completeness",
                _enum("casecompleteness", "not_case", "case_lead", "partial_case", "full_case"),
                nullable=False,
                server_default="not_case",
            )
        )
        batch_op.add_column(
            sa.Column("taxonomy_version", sa.String(length=20), nullable=False, server_default="v2")
        )
        batch_op.add_column(
            sa.Column("taxonomy_matched_rules", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(sa.Column("organizer", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("application_name", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("application_target", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("application_method", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("application_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("discovery_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("official_url", sa.String(length=2048), nullable=True))
        batch_op.add_column(sa.Column("origin_publisher", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("parent_item_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_items_parent_item_id",
            "intelligence_items",
            ["parent_item_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_intelligence_items_parent_item_id", ["parent_item_id"])
        batch_op.create_index("ix_items_primary_type_discovered", ["primary_type", "discovered_at"])

    # Only deterministic action phrases produce a v2 primary type.  Topic tags
    # are safe to derive from legacy category even when event form is ambiguous.
    op.execute(
        "UPDATE intelligence_items SET topic_tags = CASE category "
        "WHEN 'model_technology' THEN '[\"model\"]' "
        "WHEN 'agent_product' THEN '[\"agent\"]' "
        "WHEN 'award_case' THEN '[\"award\"]' "
        "WHEN 'enterprise_case' THEN '[\"case\",\"industry_application\"]' "
        "WHEN 'policy_industry' THEN '[\"policy\"]' ELSE '[]' END"
    )
    op.execute(
        "UPDATE intelligence_items SET primary_type = CASE "
        "WHEN category IN ('model_technology','agent_product') AND "
        "(title LIKE '%发布%' OR title LIKE '%上线%' OR title LIKE '%升级%' "
        "OR title LIKE '%开源%' OR title LIKE '%下线%' OR title LIKE '%退役%') "
        "THEN 'product_update' "
        "WHEN category = 'solicitation' AND (title LIKE '%征集%' "
        "OR title LIKE '%申报%' OR title LIKE '%报名%' OR title LIKE '%参评%') "
        "THEN 'application_opportunity' "
        "WHEN category = 'award_case' AND (title LIKE '%获奖%' "
        "OR title LIKE '%入选%' OR title LIKE '%名单%' OR title LIKE '%公示%' "
        "OR title LIKE '%榜单%') THEN 'award_result' "
        "WHEN category = 'policy_industry' AND (title LIKE '%政策%' "
        "OR title LIKE '%法规%' OR title LIKE '%标准%' OR title LIKE '%办法%' "
        "OR title LIKE '%征求意见%') THEN 'policy_standard' "
        "WHEN category = 'policy_industry' AND (title LIKE '%报告%' "
        "OR title LIKE '%白皮书%' OR title LIKE '%蓝皮书%' OR title LIKE '%指南%') "
        "THEN 'report_release' "
        "ELSE 'unclassified' END"
    )
    op.execute(
        "UPDATE intelligence_items SET case_completeness = 'case_lead' "
        "WHERE title LIKE '%案例%' AND category IN ('enterprise_case','award_case')"
    )
    op.execute(
        "UPDATE intelligence_items SET verification_status = 'official_confirmed', "
        "review_status = 'not_required' WHERE source_id IN "
        "(SELECT id FROM sources WHERE source_kind = 'formal')"
    )
    op.execute(
        "UPDATE intelligence_items SET manual_primary_type = primary_type "
        "WHERE manual_category IS NOT NULL AND primary_type != 'unclassified'"
    )

    op.create_table(
        "crawl_source_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("crawl_run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("new_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("crawl_run_id", "source_id", name="uq_run_source_execution"),
    )
    op.create_index(
        "ix_crawl_source_executions_crawl_run_id", "crawl_source_executions", ["crawl_run_id"]
    )
    op.create_index(
        "ix_crawl_source_executions_source_id", "crawl_source_executions", ["source_id"]
    )

    op.create_table(
        "item_review_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_source", sa.String(length=100), nullable=False),
        sa.Column("old_data", sa.JSON(), nullable=False),
        sa.Column("new_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["intelligence_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_item_review_events_item_id", "item_review_events", ["item_id"])


def downgrade() -> None:
    op.drop_index("ix_item_review_events_item_id", table_name="item_review_events")
    op.drop_table("item_review_events")
    op.drop_index("ix_crawl_source_executions_source_id", table_name="crawl_source_executions")
    op.drop_index("ix_crawl_source_executions_crawl_run_id", table_name="crawl_source_executions")
    op.drop_table("crawl_source_executions")

    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.drop_index("ix_items_primary_type_discovered")
        batch_op.drop_index("ix_intelligence_items_parent_item_id")
        batch_op.drop_constraint("fk_items_parent_item_id", type_="foreignkey")
        for constraint in (
            "primarytype",
            "manualprimarytype",
            "verificationstatus",
            "reviewstatus",
            "casecompleteness",
        ):
            batch_op.drop_constraint(constraint, type_="check")
        for name in (
            "parent_item_id",
            "origin_publisher",
            "official_url",
            "discovery_url",
            "application_url",
            "application_method",
            "deadline_at",
            "application_target",
            "application_name",
            "organizer",
            "taxonomy_matched_rules",
            "taxonomy_version",
            "case_completeness",
            "review_manually_set",
            "verification_manually_set",
            "review_status",
            "verification_status",
            "manual_industry_tags",
            "industry_tags",
            "manual_topic_tags",
            "topic_tags",
            "manual_primary_type",
            "primary_type",
        ):
            batch_op.drop_column(name)

    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_constraint("uq_sources_slug", type_="unique")
        for constraint in (
            "lifecyclestate",
            "sourcerole",
            "crawlmode",
            "reviewpolicy",
            "implementationstatus",
        ):
            batch_op.drop_constraint(constraint, type_="check")
        for name in (
            "catalog_fingerprint",
            "catalog_managed",
            "preview_result",
            "preview_item_count",
            "last_preview_at",
            "verified_at",
            "activation_evidence",
            "implementation_reason",
            "implementation_status",
            "max_items_per_run",
            "lookback_days",
            "allowed_primary_types",
            "review_policy",
            "crawl_mode",
            "source_role",
            "lifecycle_state",
            "slug",
        ):
            batch_op.drop_column(name)
