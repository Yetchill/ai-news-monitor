"""update crawl run status values

Revision ID: 8df43a9b1c2e
Revises: db0caa03a995
Create Date: 2026-07-17 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8df43a9b1c2e"
down_revision: str | None = "db0caa03a995"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Rename completed statuses; unknown legacy values fail before schema changes."""

    _require_known_statuses({"running", "succeeded", "partial", "failed"})
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_constraint("crawlstatus", type_="check")
    op.execute("UPDATE crawl_runs SET status = 'success' WHERE status = 'succeeded'")
    op.execute("UPDATE crawl_runs SET status = 'partial_success' WHERE status = 'partial'")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=9),
            type_=sa.Enum(
                "running",
                "success",
                "partial_success",
                "failed",
                name="crawlstatus",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Losslessly restore the original stage-one status names."""

    _require_known_statuses({"running", "success", "partial_success", "failed"})
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.drop_constraint("crawlstatus", type_="check")
    op.execute("UPDATE crawl_runs SET status = 'succeeded' WHERE status = 'success'")
    op.execute("UPDATE crawl_runs SET status = 'partial' WHERE status = 'partial_success'")
    with op.batch_alter_table("crawl_runs") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=15),
            type_=sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                name="crawlstatus",
                native_enum=False,
                create_constraint=True,
            ),
            existing_nullable=False,
        )


def _require_known_statuses(known: set[str]) -> None:
    statuses = set(
        op.get_bind().execute(sa.text("SELECT DISTINCT status FROM crawl_runs")).scalars()
    )
    unknown = statuses - known
    if unknown:
        rendered = ", ".join(sorted(repr(value) for value in unknown))
        raise RuntimeError(f"crawl_runs contains unknown status values: {rendered}")
