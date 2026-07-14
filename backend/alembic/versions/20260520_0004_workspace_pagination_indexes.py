"""Workspace pagination indexes and composite URL uniqueness.

Revision ID: 20260520_0004
Revises: 20260522_0003
Create Date: 2026-05-20

"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260520_0004"
down_revision: Union[str, None] = "20260522_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("competitor_products_url_key", "competitor_products", type_="unique")
    op.create_unique_constraint(
        "uq_competitor_product_url",
        "competitor_products",
        ["competitor_id", "url"],
    )
    op.create_index(
        "ix_competitor_products_competitor_category",
        "competitor_products",
        ["competitor_id", "competitor_category_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_products_title"),
        "competitor_products",
        ["title"],
        unique=False,
    )

    op.create_index(
        "ix_price_snapshots_cp_captured_at",
        "price_snapshots",
        ["competitor_product_id", "captured_at"],
        unique=False,
        postgresql_ops={"captured_at": "DESC"},
    )

    op.create_index(
        "ix_product_matches_cp_status",
        "product_matches",
        ["competitor_product_id", "status"],
        unique=False,
    )

    op.create_index(
        "ix_competitor_categories_competitor_parent",
        "competitor_categories",
        ["competitor_id", "parent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_competitor_categories_competitor_parent", table_name="competitor_categories")
    op.drop_index("ix_product_matches_cp_status", table_name="product_matches")
    op.drop_index("ix_price_snapshots_cp_captured_at", table_name="price_snapshots")
    op.drop_index(op.f("ix_competitor_products_title"), table_name="competitor_products")
    op.drop_index("ix_competitor_products_competitor_category", table_name="competitor_products")
    op.drop_constraint("uq_competitor_product_url", "competitor_products", type_="unique")
    op.create_unique_constraint("competitor_products_url_key", "competitor_products", ["url"])
