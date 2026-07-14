"""Idempotent repair: ensure product_matches batch-match columns exist.

Revision ID: 20260523_0012
Revises: 20260522_0011
Create Date: 2026-05-23

Safe to run when 20260522_0011 was stamped but columns are missing, or when
upgrading from an older DB that never received the metadata columns.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "20260523_0012"
down_revision: Union[str, None] = "20260522_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return inspect(op.get_bind())


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in _insp().get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    return name in {i["name"] for i in _insp().get_indexes(table)}


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    if not _has_column(table, column.name):
        op.add_column(table, column)


def _create_index_if_missing(name: str, table: str, columns: list[str]) -> None:
    if not _has_index(table, name):
        op.create_index(name, table, columns, unique=False)


def _json_type():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    json_type = _json_type()

    _add_column_if_missing(
        "product_matches",
        sa.Column("match_reason", sa.Text(), nullable=True),
    )
    _add_column_if_missing(
        "product_matches",
        sa.Column("match_warnings", json_type, nullable=True),
    )
    _add_column_if_missing(
        "product_matches",
        sa.Column("candidate_count", sa.Integer(), nullable=True, server_default="0"),
    )
    _add_column_if_missing(
        "product_matches",
        sa.Column("top_candidates", json_type, nullable=True),
    )
    _add_column_if_missing(
        "product_matches",
        sa.Column("matched_by", sa.String(length=64), nullable=True),
    )

    _create_index_if_missing("ix_product_matches_status", "product_matches", ["status"])
    _create_index_if_missing("ix_product_matches_match_score", "product_matches", ["match_score"])
    _create_index_if_missing("ix_product_matches_matched_by", "product_matches", ["matched_by"])


def downgrade() -> None:
    # No-op repair migration: leave columns from 0011 in place on downgrade.
    pass
