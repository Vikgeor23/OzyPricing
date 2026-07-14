"""Product and competitor product fields for richer matching.

Revision ID: 20260520_0005
Revises: 20260520_0004
Create Date: 2026-05-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "20260520_0005"
down_revision: Union[str, None] = "20260520_0004"
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
    # --- products ---
    _add_column_if_missing("products", sa.Column("model", sa.String(length=255), nullable=True))
    _add_column_if_missing("products", sa.Column("product_url", sa.Text(), nullable=True))
    _add_column_if_missing("products", sa.Column("image_url", sa.Text(), nullable=True))
    _add_column_if_missing("products", sa.Column("description", sa.Text(), nullable=True))
    _add_column_if_missing("products", sa.Column("variant", sa.String(length=255), nullable=True))
    _add_column_if_missing("products", sa.Column("color", sa.String(length=255), nullable=True))
    _add_column_if_missing("products", sa.Column("size", sa.String(length=255), nullable=True))
    _add_column_if_missing("products", sa.Column("storage", sa.String(length=128), nullable=True))
    _add_column_if_missing("products", sa.Column("memory", sa.String(length=128), nullable=True))
    _add_column_if_missing("products", sa.Column("supplier_sku", sa.String(length=255), nullable=True))

    _create_index_if_missing("ix_products_ean", "products", ["ean"])
    _create_index_if_missing("ix_products_manufacturer_code", "products", ["manufacturer_code"])
    _create_index_if_missing("ix_products_model", "products", ["model"])

    # --- competitor_products (ean/brand/sku may exist from initial migration) ---
    _add_column_if_missing("competitor_products", sa.Column("ean", sa.String(length=64), nullable=True))
    _add_column_if_missing("competitor_products", sa.Column("brand", sa.String(length=255), nullable=True))
    _add_column_if_missing(
        "competitor_products",
        sa.Column("manufacturer_code", sa.String(length=255), nullable=True),
    )
    _add_column_if_missing("competitor_products", sa.Column("model", sa.String(length=255), nullable=True))
    _add_column_if_missing(
        "competitor_products",
        sa.Column("specs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _add_column_if_missing(
        "competitor_products",
        sa.Column("raw_identifiers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    _create_index_if_missing("ix_competitor_products_ean", "competitor_products", ["ean"])
    _create_index_if_missing(
        "ix_competitor_products_manufacturer_code",
        "competitor_products",
        ["manufacturer_code"],
    )


def downgrade() -> None:
    if _has_index("competitor_products", "ix_competitor_products_manufacturer_code"):
        op.drop_index("ix_competitor_products_manufacturer_code", table_name="competitor_products")
    if _has_index("competitor_products", "ix_competitor_products_ean"):
        op.drop_index("ix_competitor_products_ean", table_name="competitor_products")

    for col in ("raw_identifiers", "specs_json", "model", "manufacturer_code"):
        if _has_column("competitor_products", col):
            op.drop_column("competitor_products", col)

    if _has_index("products", "ix_products_model"):
        op.drop_index("ix_products_model", table_name="products")
    if _has_index("products", "ix_products_manufacturer_code"):
        op.drop_index("ix_products_manufacturer_code", table_name="products")

    for col in (
        "supplier_sku",
        "memory",
        "storage",
        "size",
        "color",
        "variant",
        "description",
        "image_url",
        "product_url",
        "model",
    ):
        if _has_column("products", col):
            op.drop_column("products", col)
