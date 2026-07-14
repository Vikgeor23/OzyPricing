"""shop_code and extra_code identifier columns on competitor_products.

Revision ID: 20260713_0020
Revises: 20260712_0019
Create Date: 2026-07-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260713_0020"
down_revision: Union[str, None] = "20260712_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("competitor_products", "shop_code"):
        op.add_column(
            "competitor_products",
            sa.Column("shop_code", sa.String(length=255), nullable=True),
        )
    if not _has_column("competitor_products", "extra_code"):
        op.add_column(
            "competitor_products",
            sa.Column("extra_code", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    for col in ("extra_code", "shop_code"):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)
