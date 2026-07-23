"""add formal source metadata and admission run statistics

Revision ID: 7a8b9c0d1e2f
Revises: f2c7a93d1b44
Create Date: 2026-07-19 15:00:00.000000

Downgrade removes the new metadata and counters without deleting content. Qwen-Agent
is intentionally left disabled because its pre-migration enabled state cannot be
recovered safely; an operator may explicitly re-enable it after downgrading.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a8b9c0d1e2f"
down_revision: str | None = "f2c7a93d1b44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sources") as batch_op:
        batch_op.add_column(
            sa.Column(
                "source_kind",
                sa.Enum(
                    "formal",
                    "test",
                    "fallback",
                    name="sourcekind",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="test",
            )
        )
        batch_op.add_column(
            sa.Column(
                "source_tier",
                sa.Enum(
                    "government",
                    "official_company",
                    "association",
                    "authoritative_media",
                    "fallback",
                    name="sourcetier",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="fallback",
            )
        )
        batch_op.add_column(
            sa.Column(
                "audience",
                sa.Enum(
                    "leadership",
                    "all",
                    name="sourceaudience",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=False,
                server_default="all",
            )
        )
        batch_op.add_column(
            sa.Column("homepage_visible", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("export_visible", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("content_scope", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("include_terms", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("exclude_terms", sa.JSON(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("minimum_quality_score", sa.Float(), nullable=False, server_default="50.0")
        )
        batch_op.add_column(
            sa.Column("accept_title_only", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column(
                "allow_external_links", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "allow_technical_updates", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    with op.batch_alter_table("crawl_runs") as batch_op:
        for name in (
            "normalized_count",
            "accepted_count",
            "rejected_count",
            "classified_count",
            "duplicate_count",
            "failed_count",
        ):
            batch_op.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column("rejection_reason_counts", sa.JSON(), nullable=False, server_default="{}")
        )

    op.execute(
        "UPDATE sources SET source_kind = 'fallback', source_tier = 'fallback', "
        "homepage_visible = 0, export_visible = 0 WHERE source_type = 'github_release'"
    )
    op.execute(
        "UPDATE sources SET enabled = 0, source_kind = 'fallback', "
        "homepage_visible = 0, export_visible = 0 "
        "WHERE start_url LIKE 'https://github.com/QwenLM/Qwen-Agent/releases%'"
    )


def downgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_column("rejection_reason_counts")
        for name in (
            "failed_count",
            "duplicate_count",
            "classified_count",
            "rejected_count",
            "accepted_count",
            "normalized_count",
        ):
            batch_op.drop_column(name)

    with op.batch_alter_table("sources") as batch_op:
        batch_op.drop_constraint("sourceaudience", type_="check")
        batch_op.drop_constraint("sourcetier", type_="check")
        batch_op.drop_constraint("sourcekind", type_="check")
        for name in (
            "allow_technical_updates",
            "allow_external_links",
            "accept_title_only",
            "minimum_quality_score",
            "exclude_terms",
            "include_terms",
            "content_scope",
            "export_visible",
            "homepage_visible",
            "audience",
            "source_tier",
            "source_kind",
        ):
            batch_op.drop_column(name)
