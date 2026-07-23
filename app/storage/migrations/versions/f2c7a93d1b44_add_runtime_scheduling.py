"""add runtime scheduling settings and crawl run trigger

Revision ID: f2c7a93d1b44
Revises: c94d2a1f7e3b
Create Date: 2026-07-19 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c7a93d1b44"
down_revision: str | None = "c94d2a1f7e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    unknown_statuses = list(
        connection.execute(
            sa.text(
                "SELECT DISTINCT status FROM crawl_runs "
                "WHERE status NOT IN ('running', 'success', 'partial_success', 'failed')"
            )
        ).scalars()
    )
    if unknown_statuses:
        values = ", ".join(sorted(str(status) for status in unknown_statuses))
        raise RuntimeError(f"crawl_runs contains unknown status values: {values}")

    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger",
                sa.Enum(
                    "legacy_manual",
                    "manual_web",
                    "manual_cli",
                    "scheduled",
                    name="runtrigger",
                    native_enum=False,
                    create_constraint=True,
                ),
                nullable=True,
            )
        )
    op.execute("UPDATE crawl_runs SET trigger = 'legacy_manual' WHERE trigger IS NULL")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.alter_column("trigger", existing_type=sa.String(length=13), nullable=False)

    op.create_table(
        "schedule_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False),
        sa.Column("schedule_hour", sa.Integer(), nullable=False),
        sa.Column("schedule_minute", sa.Integer(), nullable=False),
        sa.Column("schedule_days_mask", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_scheduled_trigger_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_schedule_settings_singleton"),
        sa.CheckConstraint("schedule_hour BETWEEN 0 AND 23", name="ck_schedule_hour"),
        sa.CheckConstraint("schedule_minute BETWEEN 0 AND 59", name="ck_schedule_minute"),
        sa.CheckConstraint("schedule_days_mask BETWEEN 1 AND 127", name="ck_schedule_days"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("schedule_settings")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_constraint("runtrigger", type_="check")
        batch_op.drop_column("trigger")
