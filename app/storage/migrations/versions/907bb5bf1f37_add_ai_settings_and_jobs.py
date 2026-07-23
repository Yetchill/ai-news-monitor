"""add_ai_settings_and_jobs

Revision ID: 907bb5bf1f37
Revises: 252f80cbe271
Create Date: 2026-07-22 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "907bb5bf1f37"
down_revision: str | None = "252f80cbe271"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.Integer(), primary_key=True, default=1),
        sa.Column("provider", sa.String(30), nullable=False, server_default="deepseek"),
        sa.Column("base_url", sa.String(500), nullable=False, server_default="https://api.deepseek.com"),
        sa.Column("model", sa.String(100), nullable=False, server_default="deepseek-chat"),
        sa.Column("api_key", sa.String(500), nullable=False, server_default=""),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("classifier_mode", sa.String(20), nullable=False, server_default="off"),
        sa.Column("classifier_strategy", sa.String(20), nullable=False, server_default="hybrid"),
        sa.Column("summarizer_mode", sa.String(20), nullable=False, server_default="off"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("(datetime('now'))")),
        sa.CheckConstraint("id = 1", name="ck_ai_settings_singleton"),
        sa.CheckConstraint(
            "provider IN ('deepseek','openai','custom')",
            name="ck_ai_settings_provider",
        ),
        sa.CheckConstraint(
            "classifier_mode IN ('off','manual','auto')",
            name="ck_ai_classifier_mode",
        ),
        sa.CheckConstraint(
            "summarizer_mode IN ('off','manual','auto')",
            name="ck_ai_summarizer_mode",
        ),
    )

    op.create_table(
        "ai_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_type", sa.String(20), nullable=False),
        sa.Column("trigger", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("total_count", sa.Integer(), nullable=False, default=0),
        sa.Column("success_count", sa.Integer(), nullable=False, default=0),
        sa.Column("failure_count", sa.Integer(), nullable=False, default=0),
        sa.Column("skipped_count", sa.Integer(), nullable=False, default=0),
        sa.Column("fallback_count", sa.Integer(), nullable=False, default=0),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("(datetime('now'))")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ai_jobs")
    op.drop_table("ai_settings")
