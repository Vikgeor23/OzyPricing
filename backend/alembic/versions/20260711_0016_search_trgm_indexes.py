"""Trigram GIN indexes for workspace search (ILIKE '%term%').

The workspace search ORs ILIKE '%term%' across competitor_products and the
linked/ matched products. A leading-wildcard ILIKE is unindexable by btree, so
on large competitors (EMAG ~1.6M rows) the query seq-scanned + sort-spilled for
minutes. pg_trgm GIN indexes make each ILIKE branch index-scannable, so the OR
becomes a BitmapOr of index scans.

Revision ID: 20260711_0016
Revises: 20260711_0015
Create Date: 2026-07-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260711_0016"
down_revision: Union[str, None] = "20260711_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index_name, table, column) — every column referenced in the search OR must be
# indexed, otherwise its branch forces a full seq scan and defeats the BitmapOr.
_TRGM_INDEXES = [
    ("ix_cp_trgm_title", "competitor_products", "title"),
    ("ix_cp_trgm_url", "competitor_products", "url"),
    ("ix_cp_trgm_sku", "competitor_products", "sku"),
    ("ix_cp_trgm_ean", "competitor_products", "ean"),
    ("ix_cp_trgm_brand", "competitor_products", "brand"),
    ("ix_cp_trgm_mfr", "competitor_products", "manufacturer_code"),
    ("ix_cp_trgm_model", "competitor_products", "model"),
    ("ix_prod_trgm_name", "products", "name"),
    ("ix_prod_trgm_sku", "products", "sku"),
    ("ix_prod_trgm_ean", "products", "ean"),
    ("ix_prod_trgm_brand", "products", "brand"),
    ("ix_prod_trgm_mfr", "products", "manufacturer_code"),
    ("ix_prod_trgm_model", "products", "model"),
]


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _execute_autocommit(sql: str) -> None:
    if not _is_postgres():
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(sql))


def upgrade() -> None:
    if not _is_postgres():
        return
    # Extension first (committed) so gin_trgm_ops resolves for the index builds.
    _execute_autocommit("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _TRGM_INDEXES:
        _execute_autocommit(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
            f"ON {table} USING gin ({column} gin_trgm_ops)",
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for name, _table, _column in reversed(_TRGM_INDEXES):
        _execute_autocommit(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
