"""Workspace sort indexes for large competitor exports/lists.

Revision ID: 20260701_0013
Revises: 20260523_0012
Create Date: 2026-07-01

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260701_0013"
down_revision: Union[str, None] = "20260523_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _execute_if_postgres(sql: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(sql))


def upgrade() -> None:
    _execute_if_postgres(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cp_competitor_scraped_created "
        "ON competitor_products (competitor_id, latest_scraped_at DESC NULLS LAST, created_at DESC)",
    )
    _execute_if_postgres(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_cp_category_scraped_created "
        "ON competitor_products (competitor_category_id, latest_scraped_at DESC NULLS LAST, created_at DESC)",
    )


def downgrade() -> None:
    _execute_if_postgres("DROP INDEX CONCURRENTLY IF EXISTS ix_cp_category_scraped_created")
    _execute_if_postgres("DROP INDEX CONCURRENTLY IF EXISTS ix_cp_competitor_scraped_created")
