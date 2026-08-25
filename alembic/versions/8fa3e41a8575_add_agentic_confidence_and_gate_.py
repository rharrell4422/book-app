"""add agentic_confidence_decisions and agentic_gate_decisions tables

Phase 2 dual-write shadow tables (final Phase 2 scaffolding block; see
`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s
settled architecture, not re-litigated here): two new tables pairing the
live pipeline's confidence/gate outcome for a book against the Phase 1
shadow loop's (`agents/agentic_series_agent.run_agentic_turn`)
`confidence_traces`/`gate_traces` entry for the same book, one row per
dry-run turn. Purely diagnostic -- entirely separate from the live
`series_skeleton` table and live confidence/gate logic; see
`services/agentic_confidence_gate_store.py` for the only read/write path.

Revision ID: 8fa3e41a8575
Revises: a9443ae2172e
Create Date: 2026-08-24 21:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '8fa3e41a8575'
down_revision: Union[str, Sequence[str], None] = 'a9443ae2172e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agentic_confidence_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("book_number", sa.Float(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("live_confidence", sqlite.JSON(), nullable=False),
        sa.Column("agentic_confidence", sqlite.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agentic_confidence_decisions_series_id"),
        "agentic_confidence_decisions",
        ["series_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agentic_confidence_decisions_book_number"),
        "agentic_confidence_decisions",
        ["book_number"],
        unique=False,
    )
    op.create_index(
        "ix_agentic_confidence_decisions_series_id_book_number",
        "agentic_confidence_decisions",
        ["series_id", "book_number"],
        unique=False,
    )

    op.create_table(
        "agentic_gate_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("book_number", sa.Float(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("live_gate", sqlite.JSON(), nullable=False),
        sa.Column("agentic_gate", sqlite.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agentic_gate_decisions_series_id"), "agentic_gate_decisions", ["series_id"], unique=False
    )
    op.create_index(
        op.f("ix_agentic_gate_decisions_book_number"), "agentic_gate_decisions", ["book_number"], unique=False
    )
    op.create_index(
        "ix_agentic_gate_decisions_series_id_book_number",
        "agentic_gate_decisions",
        ["series_id", "book_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agentic_gate_decisions_series_id_book_number", table_name="agentic_gate_decisions")
    op.drop_index(op.f("ix_agentic_gate_decisions_book_number"), table_name="agentic_gate_decisions")
    op.drop_index(op.f("ix_agentic_gate_decisions_series_id"), table_name="agentic_gate_decisions")
    op.drop_table("agentic_gate_decisions")

    op.drop_index(
        "ix_agentic_confidence_decisions_series_id_book_number", table_name="agentic_confidence_decisions"
    )
    op.drop_index(op.f("ix_agentic_confidence_decisions_book_number"), table_name="agentic_confidence_decisions")
    op.drop_index(op.f("ix_agentic_confidence_decisions_series_id"), table_name="agentic_confidence_decisions")
    op.drop_table("agentic_confidence_decisions")
