"""Latest scrape columns on competitor_products.

Revision ID: 20260521_0007
Revises: 20260520_0006
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260521_0007"
down_revision: Union[str, None] = "20260520_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _insp().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _insp().get_indexes(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _create_index_if_missing(name: str, table: str, columns: list[str], *, postgresql_ops: dict | None = None) -> None:
    if _has_index(table, name):
        return
    kwargs: dict = {}
    if postgresql_ops:
        kwargs["postgresql_ops"] = postgresql_ops
    op.create_index(name, table, columns, **kwargs)


def upgrade() -> None:
    _add_column_if_missing("competitor_products", sa.Column("latest_price", sa.Numeric(14, 4), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("latest_old_price", sa.Numeric(14, 4), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("latest_promo_price", sa.Numeric(14, 4), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("latest_currency", sa.String(8), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("latest_availability", sa.String(128), nullable=True))
    _add_column_if_missing(
        "competitor_products",
        sa.Column("latest_scraped_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing("competitor_products", sa.Column("latest_scrape_status", sa.String(32), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("latest_scrape_error", sa.Text(), nullable=True))

    _create_index_if_missing("ix_competitor_products_latest_scraped_at", "competitor_products", ["latest_scraped_at"])
    _create_index_if_missing(
        "ix_competitor_products_competitor_latest_scraped",
        "competitor_products",
        ["competitor_id", "latest_scraped_at"],
        postgresql_ops={"latest_scraped_at": "DESC"},
    )
    _create_index_if_missing(
        "ix_competitor_products_category_latest_scraped",
        "competitor_products",
        ["competitor_category_id", "latest_scraped_at"],
        postgresql_ops={"latest_scraped_at": "DESC"},
    )


def downgrade() -> None:
    for name in (
        "ix_competitor_products_category_latest_scraped",
        "ix_competitor_products_competitor_latest_scraped",
        "ix_competitor_products_latest_scraped_at",
    ):
        if _has_index("competitor_products", name):
            op.drop_index(name, table_name="competitor_products")

    for col in (
        "latest_scrape_error",
        "latest_scrape_status",
        "latest_scraped_at",
        "latest_availability",
        "latest_currency",
        "latest_promo_price",
        "latest_old_price",
        "latest_price",
    ):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)
