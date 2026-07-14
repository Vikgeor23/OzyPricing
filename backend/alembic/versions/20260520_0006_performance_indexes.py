"""Performance indexes and optional pg_trgm for name/title search.

Revision ID: 20260520_0006
Revises: 20260520_0005
Create Date: 2026-05-20

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "20260520_0006"
down_revision: Union[str, None] = "20260520_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return inspect(op.get_bind())


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _insp().get_indexes(table)}


def _create_index_if_missing(
    name: str,
    table: str,
    columns: list[str],
    *,
    unique: bool = False,
    postgresql_ops: dict | None = None,
) -> None:
    if _has_index(table, name):
        return
    kwargs: dict = {"unique": unique}
    if postgresql_ops:
        kwargs["postgresql_ops"] = postgresql_ops
    op.create_index(name, table, columns, **kwargs)


def _execute_if_postgres(sql: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(sql))


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        _execute_if_postgres("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # --- products ---
    _create_index_if_missing("ix_products_sku", "products", ["sku"])
    _create_index_if_missing("ix_products_brand", "products", ["brand"])
    _create_index_if_missing("ix_products_category", "products", ["category"])
    _create_index_if_missing("ix_products_tenant_id", "products", ["tenant_id"])
    if is_pg:
        _execute_if_postgres(
            "CREATE INDEX IF NOT EXISTS ix_products_name_lower ON products (lower(name))",
        )
        _execute_if_postgres(
            "CREATE INDEX IF NOT EXISTS ix_products_name_trgm "
            "ON products USING gin (name gin_trgm_ops)",
        )

    # --- competitors ---
    _create_index_if_missing("ix_competitors_name", "competitors", ["name"])

    # --- competitor_categories ---
    _create_index_if_missing(
        "ix_competitor_categories_competitor_url",
        "competitor_categories",
        ["competitor_id", "url"],
    )
    _create_index_if_missing(
        "ix_competitor_categories_competitor_path",
        "competitor_categories",
        ["competitor_id", "path"],
    )

    # --- competitor_products ---
    _create_index_if_missing("ix_competitor_products_brand", "competitor_products", ["brand"])
    _create_index_if_missing("ix_competitor_products_model", "competitor_products", ["model"])
    _create_index_if_missing(
        "ix_competitor_products_last_seen_at",
        "competitor_products",
        ["last_seen_at"],
    )
    _create_index_if_missing(
        "ix_competitor_products_competitor_last_seen",
        "competitor_products",
        ["competitor_id", "last_seen_at"],
    )
    _create_index_if_missing(
        "ix_competitor_products_competitor_title",
        "competitor_products",
        ["competitor_id", "title"],
    )
    if is_pg:
        _execute_if_postgres(
            "CREATE INDEX IF NOT EXISTS ix_competitor_products_title_trgm "
            "ON competitor_products USING gin (title gin_trgm_ops)",
        )

    # --- price_snapshots ---
    _create_index_if_missing(
        "ix_price_snapshots_captured_at",
        "price_snapshots",
        ["captured_at"],
    )
    _create_index_if_missing(
        "ix_price_snapshots_currency",
        "price_snapshots",
        ["currency"],
    )

    # --- product_matches ---
    _create_index_if_missing("ix_product_matches_status", "product_matches", ["status"])
    _create_index_if_missing(
        "ix_product_matches_product_status",
        "product_matches",
        ["product_id", "status"],
    )
    _create_index_if_missing(
        "ix_product_matches_match_score",
        "product_matches",
        ["match_score"],
    )
    _create_index_if_missing(
        "ix_product_matches_status_score",
        "product_matches",
        ["status", "match_score"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    for name in (
        "ix_product_matches_status_score",
        "ix_product_matches_match_score",
        "ix_product_matches_product_status",
        "ix_product_matches_status",
    ):
        if _has_index("product_matches", name):
            op.drop_index(name, table_name="product_matches")

    for name in ("ix_price_snapshots_currency", "ix_price_snapshots_captured_at"):
        if _has_index("price_snapshots", name):
            op.drop_index(name, table_name="price_snapshots")

    for name in (
        "ix_competitor_products_competitor_title",
        "ix_competitor_products_competitor_last_seen",
        "ix_competitor_products_last_seen_at",
        "ix_competitor_products_model",
        "ix_competitor_products_brand",
    ):
        if _has_index("competitor_products", name):
            op.drop_index(name, table_name="competitor_products")

    if is_pg:
        _execute_if_postgres("DROP INDEX IF EXISTS ix_competitor_products_title_trgm")

    for name in (
        "ix_competitor_categories_competitor_path",
        "ix_competitor_categories_competitor_url",
    ):
        if _has_index("competitor_categories", name):
            op.drop_index(name, table_name="competitor_categories")

    if _has_index("competitors", "ix_competitors_name"):
        op.drop_index("ix_competitors_name", table_name="competitors")

    if is_pg:
        _execute_if_postgres("DROP INDEX IF EXISTS ix_products_name_trgm")
        _execute_if_postgres("DROP INDEX IF EXISTS ix_products_name_lower")

    for name in ("ix_products_tenant_id", "ix_products_category", "ix_products_brand", "ix_products_sku"):
        if _has_index("products", name):
            op.drop_index(name, table_name="products")
