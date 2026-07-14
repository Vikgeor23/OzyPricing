"""Competitor category tree + optional FK on listings.

Revision ID: 20260522_0003
Revises: 20240521_0002
Create Date: 2026-05-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260522_0003"
down_revision: Union[str, None] = "20240521_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "competitor_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("competitor_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("path", sa.String(length=1024), nullable=True),
        sa.Column("product_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["competitor_id"], ["competitors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["competitor_categories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("competitor_id", "url", name="uq_competitor_category_url"),
    )
    op.create_index(
        op.f("ix_competitor_categories_competitor_id"),
        "competitor_categories",
        ["competitor_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_competitor_categories_parent_id"),
        "competitor_categories",
        ["parent_id"],
        unique=False,
    )

    op.add_column(
        "competitor_products",
        sa.Column("competitor_category_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f("ix_competitor_products_competitor_category_id"),
        "competitor_products",
        ["competitor_category_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_competitor_products_category",
        "competitor_products",
        "competitor_categories",
        ["competitor_category_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_competitor_products_category", "competitor_products", type_="foreignkey")
    op.drop_index(op.f("ix_competitor_products_competitor_category_id"), table_name="competitor_products")
    op.drop_column("competitor_products", "competitor_category_id")
    op.drop_index(op.f("ix_competitor_categories_parent_id"), table_name="competitor_categories")
    op.drop_index(op.f("ix_competitor_categories_competitor_id"), table_name="competitor_categories")
    op.drop_table("competitor_categories")
