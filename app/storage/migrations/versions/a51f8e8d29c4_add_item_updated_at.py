"""add intelligence item updated timestamp

Revision ID: a51f8e8d29c4
Revises: 8df43a9b1c2e
Create Date: 2026-07-18 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a51f8e8d29c4"
down_revision: str | None = "8df43a9b1c2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a durable timestamp without rewriting existing content semantics."""

    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE intelligence_items SET updated_at = "
        "COALESCE(last_seen_at, discovered_at, CURRENT_TIMESTAMP)"
    )
    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.alter_column(
            "updated_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )


def downgrade() -> None:
    """Remove the application-maintained timestamp."""

    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.drop_column("updated_at")
