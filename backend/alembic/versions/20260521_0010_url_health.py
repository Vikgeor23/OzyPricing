"""URL health fields on competitor_products.

Revision ID: 20260521_0010
Revises: 20260521_0009
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260521_0010"
down_revision: Union[str, None] = "20260521_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("competitor_products", "is_dead"):
        op.add_column(
            "competitor_products",
            sa.Column("is_dead", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _has_column("competitor_products", "consecutive_timeout_count"):
        op.add_column(
            "competitor_products",
            sa.Column("consecutive_timeout_count", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("competitor_products", "consecutive_not_found_count"):
        op.add_column(
            "competitor_products",
            sa.Column("consecutive_not_found_count", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for col in ("consecutive_not_found_count", "consecutive_timeout_count", "is_dead"):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)
