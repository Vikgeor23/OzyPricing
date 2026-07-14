"""Add manufacturer_code to products.

Revision ID: 20240521_0002
Revises: 20240520_0001
Create Date: 2026-05-21

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20240521_0002"
down_revision: Union[str, None] = "20240520_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("manufacturer_code", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "manufacturer_code")
