"""add series_skeleton table

Phase 1 of agentic discovery (see project design chat): a new
`series_skeleton` table giving Discovery a durable, per-book memory of each
series' lineup, independent of any single Check Now run. Today the only
durable state is on `series` itself (missing_books, last_checked, etc.) --
a result summary, not a record of individual books with confidence or
provenance.

This migration only creates the table. It is not populated, read, or wired
into any request path yet -- see services/skeleton_store.py for the
deterministic (zero-LLM) backfill from existing Book rows, run once on
boot the same way backfill_series_state already is.

Revision ID: 7ff0897d2bc0
Revises: a4f27c81de93
Create Date: 2026-08-19 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '7ff0897d2bc0'
down_revision: Union[str, Sequence[str], None] = 'a4f27c81de93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "series_skeleton",
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("skeleton_json", sqlite.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("series_id"),
    )


def downgrade() -> None:
    op.drop_table("series_skeleton")
