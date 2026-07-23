"""separate runtime failures and mark items admitted by stage eight

Revision ID: b6f4e2d9a731
Revises: 7a8b9c0d1e2f
Create Date: 2026-07-19 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6f4e2d9a731"
down_revision: str | None = "7a8b9c0d1e2f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.add_column(
            sa.Column("failure_reason_counts", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.add_column(
            sa.Column("admission_accepted", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("intelligence_items") as batch_op:
        batch_op.drop_column("admission_accepted")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_column("failure_reason_counts")
