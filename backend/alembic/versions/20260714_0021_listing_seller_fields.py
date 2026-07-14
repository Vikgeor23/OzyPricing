"""offered_by / delivered_by seller columns on competitor_products (eMAG marketplace).

Revision ID: 20260714_0021
Revises: 20260713_0020
Create Date: 2026-07-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260714_0021"
down_revision: Union[str, None] = "20260713_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("competitor_products", "latest_offered_by"):
        op.add_column(
            "competitor_products",
            sa.Column("latest_offered_by", sa.String(length=255), nullable=True),
        )
    if not _has_column("competitor_products", "latest_delivered_by"):
        op.add_column(
            "competitor_products",
            sa.Column("latest_delivered_by", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    for col in ("latest_delivered_by", "latest_offered_by"):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)
