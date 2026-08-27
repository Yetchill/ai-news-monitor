"""add_multi_provider_ai_workflow

Revision ID: 3c91e2a7b5d4
Revises: 4f8a2d1c6b70
Create Date: 2026-07-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c91e2a7b5d4"
down_revision: str | None = "4f8a2d1c6b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_settings",
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("api_key", sa.String(500), nullable=False, server_default=""),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("(datetime('now'))"),
        ),
    )

    with op.batch_alter_table("ai_settings") as batch_op:
        batch_op.add_column(
            sa.Column("selected_provider", sa.String(30), nullable=False, server_default="deepseek")
        )
        batch_op.add_column(
            sa.Column(
                "classification_provider",
                sa.String(30),
                nullable=False,
                server_default="deepseek",
            )
        )
        batch_op.add_column(
            sa.Column(
                "classification_model",
                sa.String(100),
                nullable=False,
                server_default="deepseek-chat",
            )
        )
        batch_op.add_column(
            sa.Column(
                "summarization_provider",
                sa.String(30),
                nullable=False,
                server_default="deepseek",
            )
        )
        batch_op.add_column(
            sa.Column(
                "summarization_model",
                sa.String(100),
                nullable=False,
                server_default="deepseek-chat",
            )
        )
        batch_op.add_column(
            sa.Column(
                "classification_batch_mode",
                sa.String(20),
                nullable=False,
                server_default="smart",
            )
        )

    with op.batch_alter_table("ai_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("current_batch", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("total_batches", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("provider", sa.String(30), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("classification_mode", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("request_signature", sa.String(100), nullable=True))
        batch_op.create_index("ix_ai_jobs_request_signature", ["request_signature"], unique=False)

    provider_table = sa.table(
        "ai_provider_settings",
        sa.column("provider", sa.String),
        sa.column("base_url", sa.String),
        sa.column("model", sa.String),
        sa.column("api_key", sa.String),
        sa.column("timeout_seconds", sa.Integer),
        sa.column("max_retries", sa.Integer),
        sa.column("enabled", sa.Boolean),
    )
    op.bulk_insert(
        provider_table,
        [
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "api_key": "",
                "timeout_seconds": 30,
                "max_retries": 1,
                "enabled": True,
            },
            {
                "provider": "openai",
                "base_url": "https://api.openai.com",
                "model": "gpt-4.1-mini",
                "api_key": "",
                "timeout_seconds": 30,
                "max_retries": 1,
                "enabled": True,
            },
            {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api",
                "model": "openai/gpt-4.1-mini",
                "api_key": "",
                "timeout_seconds": 30,
                "max_retries": 1,
                "enabled": True,
            },
            {
                "provider": "custom",
                "base_url": "",
                "model": "",
                "api_key": "",
                "timeout_seconds": 30,
                "max_retries": 1,
                "enabled": True,
            },
        ],
    )

    # Move the singleton credential to its selected provider, then erase the
    # legacy shared copy so future provider switches cannot reuse it.
    connection = op.get_bind()
    legacy = (
        connection.execute(
            sa.text(
                "SELECT provider, base_url, model, api_key, timeout_seconds, max_retries "
                "FROM ai_settings WHERE id = 1"
            )
        )
        .mappings()
        .first()
    )
    if legacy is not None:
        provider = str(legacy["provider"] or "deepseek")
        existing = connection.execute(
            sa.text("SELECT provider FROM ai_provider_settings WHERE provider = :provider"),
            {"provider": provider},
        ).first()
        values = {
            "provider": provider,
            "base_url": str(legacy["base_url"] or ""),
            "model": str(legacy["model"] or ""),
            "api_key": str(legacy["api_key"] or ""),
            "timeout_seconds": int(legacy["timeout_seconds"] or 30),
            "max_retries": int(legacy["max_retries"] or 1),
        }
        if existing is None:
            connection.execute(
                sa.text(
                    "INSERT INTO ai_provider_settings "
                    "(provider, base_url, model, api_key, timeout_seconds, max_retries, enabled) "
                    "VALUES (:provider, :base_url, :model, :api_key, :timeout_seconds, "
                    ":max_retries, 1)"
                ),
                values,
            )
        else:
            connection.execute(
                sa.text(
                    "UPDATE ai_provider_settings SET base_url=:base_url, model=:model, "
                    "api_key=:api_key, timeout_seconds=:timeout_seconds, "
                    "max_retries=:max_retries WHERE provider=:provider"
                ),
                values,
            )
        connection.execute(
            sa.text(
                "UPDATE ai_settings SET selected_provider=:provider, "
                "classification_provider=:provider, classification_model=:model, "
                "summarization_provider=:provider, summarization_model=:model, api_key='' "
                "WHERE id=1"
            ),
            {"provider": provider, "model": values["model"]},
        )


def downgrade() -> None:
    with op.batch_alter_table("ai_jobs") as batch_op:
        batch_op.drop_index("ix_ai_jobs_request_signature")
        batch_op.drop_column("request_signature")
        batch_op.drop_column("classification_mode")
        batch_op.drop_column("provider")
        batch_op.drop_column("total_batches")
        batch_op.drop_column("current_batch")
        batch_op.drop_column("processed_count")

    with op.batch_alter_table("ai_settings") as batch_op:
        batch_op.drop_column("classification_batch_mode")
        batch_op.drop_column("summarization_model")
        batch_op.drop_column("summarization_provider")
        batch_op.drop_column("classification_model")
        batch_op.drop_column("classification_provider")
        batch_op.drop_column("selected_provider")

    op.drop_table("ai_provider_settings")
