"""Drop redundant/unused indexes on hot tables.

pg_stat_user_indexes showed these with zero scans since cluster init (stats
never reset), several being exact duplicates from earlier migrations. On
competitor_products every batch-scrape UPDATE is non-HOT (latest_scraped_at is
indexed), so each row update writes to every index — dropping the dead ones
cuts write amplification by ~25% and reclaims ~400 MB.

Also tightens autovacuum on the two update-heavy tables: the default 20%
scale factor means 3.1M-row competitor_products vacuums only after ~620k dead
rows.

Revision ID: 20260712_0018
Revises: 20260712_0017
Create Date: 2026-07-12

"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260712_0018"
down_revision: Union[str, None] = "20260712_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index_name, table, definition-for-downgrade)
_DROPPED = [
    ("ix_products_name_trgm", "products", "USING gin (name gin_trgm_ops)"),  # dup of ix_prod_trgm_name
    ("ix_products_name_lower", "products", "USING btree (lower((name)::text))"),
    ("ix_competitor_products_title_trgm", "competitor_products", "USING gin (title gin_trgm_ops)"),  # dup of ix_cp_trgm_title
    ("ix_competitor_products_competitor_title", "competitor_products", "USING btree (competitor_id, title)"),
    ("ix_competitor_products_title", "competitor_products", "USING btree (title)"),
    ("ix_competitor_products_competitor_last_seen", "competitor_products", "USING btree (competitor_id, last_seen_at)"),
    ("ix_competitor_products_last_seen_at", "competitor_products", "USING btree (last_seen_at)"),
    ("ix_competitor_products_category_latest_scraped", "competitor_products", "USING btree (competitor_category_id, latest_scraped_at)"),
    ("ix_competitor_products_brand", "competitor_products", "USING btree (brand)"),
    ("ix_competitor_products_manufacturer_code", "competitor_products", "USING btree (manufacturer_code)"),
    ("ix_competitor_products_model", "competitor_products", "USING btree (model)"),
]


def upgrade() -> None:
    for name, _table, _definition in _DROPPED:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute(
        "ALTER TABLE competitor_products SET (autovacuum_vacuum_scale_factor=0.05, autovacuum_analyze_scale_factor=0.02)",
    )
    op.execute(
        "ALTER TABLE product_matches SET (autovacuum_vacuum_scale_factor=0.05, autovacuum_analyze_scale_factor=0.02)",
    )


def downgrade() -> None:
    for name, table, definition in _DROPPED:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} {definition}")
    op.execute("ALTER TABLE competitor_products RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)")
    op.execute("ALTER TABLE product_matches RESET (autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)")
