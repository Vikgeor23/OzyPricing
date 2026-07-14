"""Per-competitor scrape concurrency cap.

NULL means "use the global SCRAPE_*_CONCURRENCY_MAX setting"; a value caps the
adaptive controller for batch scrapes of that site, so aggressive limits can be
tested per retailer from the UI without redeploying.

Revision ID: 20260712_0017
Revises: 20260711_0016
Create Date: 2026-07-12

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260712_0017"
down_revision: Union[str, None] = "20260711_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("competitors", sa.Column("scrape_concurrency_max", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("competitors", "scrape_concurrency_max")
