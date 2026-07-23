"""add_read_status_and_ai_summary

Revision ID: 252f80cbe271
Revises: d4e5f6a7b8c9
Create Date: 2026-07-22 09:35:14.547817
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "252f80cbe271"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.add_column(
            sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        batch_op.add_column(
            sa.Column("ai_summary", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("ai_summary_model", sa.String(100), nullable=True),
        )
        batch_op.create_index("ix_items_is_read", ["is_read"])


def downgrade() -> None:
    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.drop_index("ix_items_is_read")
        batch_op.drop_column("ai_summary_model")
        batch_op.drop_column("ai_summary")
        batch_op.drop_column("is_read")
