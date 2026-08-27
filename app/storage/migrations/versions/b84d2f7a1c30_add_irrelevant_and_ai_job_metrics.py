"""add irrelevant category and bounded AI job metrics

Revision ID: b84d2f7a1c30
Revises: 3c91e2a7b5d4
Create Date: 2026-08-27 09:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b84d2f7a1c30"
down_revision: str | None = "3c91e2a7b5d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CATEGORIES = (
    "model_technology",
    "agent_product",
    "enterprise_case",
    "award_case",
    "solicitation",
    "policy_industry",
    "unclassified",
)
NEW_CATEGORIES = (*OLD_CATEGORIES[:-1], "irrelevant", "unclassified")


def _category(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="category", native_enum=False, create_constraint=True)


def upgrade() -> None:
    with op.batch_alter_table("sources", recreate="always") as batch_op:
        batch_op.alter_column(
            "default_category",
            existing_type=_category(OLD_CATEGORIES),
            type_=_category(NEW_CATEGORIES),
            existing_nullable=True,
        )

    with op.batch_alter_table("intelligence_items", recreate="always") as batch_op:
        batch_op.alter_column(
            "category",
            existing_type=_category(OLD_CATEGORIES),
            type_=_category(NEW_CATEGORIES),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "manual_category",
            existing_type=_category(OLD_CATEGORIES),
            type_=_category(NEW_CATEGORIES),
            existing_nullable=True,
        )

    with op.batch_alter_table("ai_jobs") as batch_op:
        batch_op.add_column(
            sa.Column("model_request_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("parse_failure_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("split_count", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    connection = op.get_bind()
    incompatible = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM intelligence_items "
            "WHERE category='irrelevant' OR manual_category='irrelevant'"
        )
    ).scalar_one()
    source_incompatible = connection.execute(
        sa.text("SELECT COUNT(*) FROM sources WHERE default_category='irrelevant'")
    ).scalar_one()
    if incompatible or source_incompatible:
        raise RuntimeError("cannot downgrade while irrelevant category records exist")

    with op.batch_alter_table("ai_jobs") as batch_op:
        batch_op.drop_column("split_count")
        batch_op.drop_column("parse_failure_count")
        batch_op.drop_column("retry_count")
        batch_op.drop_column("model_request_count")

    with op.batch_alter_table("intelligence_items", recreate="always") as batch_op:
        batch_op.alter_column(
            "category",
            existing_type=_category(NEW_CATEGORIES),
            type_=_category(OLD_CATEGORIES),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "manual_category",
            existing_type=_category(NEW_CATEGORIES),
            type_=_category(OLD_CATEGORIES),
            existing_nullable=True,
        )

    with op.batch_alter_table("sources", recreate="always") as batch_op:
        batch_op.alter_column(
            "default_category",
            existing_type=_category(NEW_CATEGORIES),
            type_=_category(OLD_CATEGORIES),
            existing_nullable=True,
        )
