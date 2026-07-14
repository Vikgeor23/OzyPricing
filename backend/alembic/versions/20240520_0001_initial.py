"""Initial schema for price monitor MVP.

Revision ID: 20240520_0001
Revises:
Create Date: 2024-05-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20240520_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("sku", sa.String(length=255), nullable=False),
        sa.Column("ean", sa.String(length=64), nullable=True),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("category", sa.String(length=255), nullable=True),
        sa.Column("own_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("cost_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("stock_quantity", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_products_ean"), "products", ["ean"], unique=False)
    op.create_index(op.f("ix_products_sku"), "products", ["sku"], unique=False)
    op.create_index(op.f("ix_products_tenant_id"), "products", ["tenant_id"], unique=False)

    op.create_table(
        "competitors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_competitors_domain"), "competitors", ["domain"], unique=False)

    op.create_table(
        "competitor_products",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("competitor_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("ean", sa.String(length=64), nullable=True),
        sa.Column("sku", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_index(op.f("ix_competitor_products_competitor_id"), "competitor_products", ["competitor_id"], unique=False)
    op.create_index(op.f("ix_competitor_products_product_id"), "competitor_products", ["product_id"], unique=False)

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("competitor_product_id", sa.Uuid(), nullable=False),
        sa.Column("price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("old_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("promo_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("availability", sa.String(length=128), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["competitor_product_id"], ["competitor_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_price_snapshots_competitor_product_id"),
        "price_snapshots",
        ["competitor_product_id"],
        unique=False,
    )

    op.create_table(
        "product_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("competitor_product_id", sa.Uuid(), nullable=False),
        sa.Column("match_score", sa.Numeric(precision=8, scale=5), nullable=False),
        sa.Column("match_method", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["competitor_product_id"], ["competitor_products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "competitor_product_id", name="uq_product_competitor_product"),
    )
    op.create_index(op.f("ix_product_matches_competitor_product_id"), "product_matches", ["competitor_product_id"], unique=False)
    op.create_index(op.f("ix_product_matches_product_id"), "product_matches", ["product_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_product_matches_product_id"), table_name="product_matches")
    op.drop_index(op.f("ix_product_matches_competitor_product_id"), table_name="product_matches")
    op.drop_table("product_matches")
    op.drop_index(op.f("ix_price_snapshots_competitor_product_id"), table_name="price_snapshots")
    op.drop_table("price_snapshots")
    op.drop_index(op.f("ix_competitor_products_product_id"), table_name="competitor_products")
    op.drop_index(op.f("ix_competitor_products_competitor_id"), table_name="competitor_products")
    op.drop_table("competitor_products")
    op.drop_index(op.f("ix_competitors_domain"), table_name="competitors")
    op.drop_table("competitors")
    op.drop_index(op.f("ix_products_tenant_id"), table_name="products")
    op.drop_index(op.f("ix_products_sku"), table_name="products")
    op.drop_index(op.f("ix_products_ean"), table_name="products")
    op.drop_table("products")
