"""Import batches for catalog uploads.

Revision ID: 20260711_0015
Revises: 20260711_0014
Create Date: 2026-07-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260711_0015"
down_revision: Union[str, None] = "20260711_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("imported_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("products", sa.Column("import_batch_id", sa.Uuid(as_uuid=True), nullable=True))
    op.create_index("ix_products_import_batch_id", "products", ["import_batch_id"])
    op.create_foreign_key(
        "fk_products_import_batch",
        "products",
        "import_batches",
        ["import_batch_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_products_import_batch", "products", type_="foreignkey")
    op.drop_index("ix_products_import_batch_id", table_name="products")
    op.drop_column("products", "import_batch_id")
    op.drop_table("import_batches")
