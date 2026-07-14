"""latest_scrape_error_code on competitor_products.

Revision ID: 20260521_0009
Revises: 20260521_0008
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260521_0009"
down_revision: Union[str, None] = "20260521_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if not _has_column("competitor_products", "latest_scrape_error_code"):
        op.add_column(
            "competitor_products",
            sa.Column("latest_scrape_error_code", sa.String(length=32), nullable=True),
        )
    bind = op.get_bind()
    indexes = {i["name"] for i in inspect(bind).get_indexes("competitor_products")}
    if "ix_competitor_products_latest_scrape_error_code" not in indexes:
        op.create_index(
            "ix_competitor_products_latest_scrape_error_code",
            "competitor_products",
            ["latest_scrape_error_code"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {i["name"] for i in inspect(bind).get_indexes("competitor_products")}
    if "ix_competitor_products_latest_scrape_error_code" in indexes:
        op.drop_index("ix_competitor_products_latest_scrape_error_code", table_name="competitor_products")
    if _has_column("competitor_products", "latest_scrape_error_code"):
        op.drop_column("competitor_products", "latest_scrape_error_code")
