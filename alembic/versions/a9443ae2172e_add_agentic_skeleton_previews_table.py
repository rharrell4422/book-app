"""add agentic_skeleton_previews table

Phase 2 dual-write shadow table (see `discovery_agentic_phase1_plan.md`/
`discovery_agentic_phase1_evaluation.md`'s settled architecture, not
re-litigated here): a new `agentic_skeleton_previews` table storing one
row per dry-run turn's `skeleton_merge_previews` output from the Phase 1
shadow loop (`agents/agentic_series_agent.run_agentic_turn`). Purely
diagnostic -- entirely separate from the live `series_skeleton` table;
see `services/agentic_skeleton_preview_store.py` for the only read/write
path.

Revision ID: a9443ae2172e
Revises: 5414264c11af
Create Date: 2026-08-24 21:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'a9443ae2172e'
down_revision: Union[str, Sequence[str], None] = '5414264c11af'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agentic_skeleton_previews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("preview_json", sqlite.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agentic_skeleton_previews_series_id"),
        "agentic_skeleton_previews",
        ["series_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_agentic_skeleton_previews_series_id"), table_name="agentic_skeleton_previews")
    op.drop_table("agentic_skeleton_previews")
