"""Discovery metadata columns on competitor_products.

Revision ID: 20260521_0008
Revises: 20260521_0007
Create Date: 2026-05-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260521_0008"
down_revision: Union[str, None] = "20260521_0007"
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


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns, unique=False)


def upgrade() -> None:
    _add_column_if_missing(
        "competitor_products",
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_column_if_missing(
        "competitor_products",
        sa.Column("discovery_source", sa.String(length=64), nullable=True),
    )
    _add_column_if_missing(
        "competitor_products",
        sa.Column("technopolis_product_code", sa.String(length=64), nullable=True),
    )
    _create_index_if_missing("ix_competitor_products_technopolis_product_code", "competitor_products", ["technopolis_product_code"])
    _create_index_if_missing(
        "ix_competitor_products_competitor_technopolis_code",
        "competitor_products",
        ["competitor_id", "technopolis_product_code"],
    )


def downgrade() -> None:
    for name in (
        "ix_competitor_products_competitor_technopolis_code",
        "ix_competitor_products_technopolis_product_code",
    ):
        if _has_index("competitor_products", name):
            op.drop_index(name, table_name="competitor_products")

    for col in ("technopolis_product_code", "discovery_source", "discovered_at"):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)
