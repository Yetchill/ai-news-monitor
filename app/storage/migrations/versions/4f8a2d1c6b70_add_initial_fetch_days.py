"""add persisted initial fetch time range

Revision ID: 4f8a2d1c6b70
Revises: 907bb5bf1f37
Create Date: 2026-07-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f8a2d1c6b70"
down_revision: str | None = "907bb5bf1f37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedule_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "initial_fetch_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("30"),
            )
        )
        batch_op.create_check_constraint(
            "ck_schedule_initial_fetch_days",
            "initial_fetch_days BETWEEN 1 AND 365",
        )


def downgrade() -> None:
    with op.batch_alter_table("schedule_settings") as batch_op:
        batch_op.drop_constraint("ck_schedule_initial_fetch_days", type_="check")
        batch_op.drop_column("initial_fetch_days")
