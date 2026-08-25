"""add agentic_promotion_decisions table

Phase 3 (final scaffolding step before full activation; see
`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s
settled architecture, not re-litigated here): the first table recording an
actual live-routing decision (`promotion_outcome`) alongside the live vs
agentic confidence/gate pair it was based on. Written only by
`services/agentic_promotion_evaluator.py`, called only from
`agents/series_agent.py`'s live routing path when
`settings.AGENTIC_ROUTING_ENABLED` is on. Purely additive/diagnostic --
entirely separate from `series_skeleton`/`agentic_confidence_decisions`/
`agentic_gate_decisions`.

Revision ID: 120718346871
Revises: 8fa3e41a8575
Create Date: 2026-08-24 21:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '120718346871'
down_revision: Union[str, Sequence[str], None] = '8fa3e41a8575'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agentic_promotion_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("book_number", sa.Float(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.Column("live_confidence", sqlite.JSON(), nullable=False),
        sa.Column("agentic_confidence", sqlite.JSON(), nullable=False),
        sa.Column("live_gate", sqlite.JSON(), nullable=False),
        sa.Column("agentic_gate", sqlite.JSON(), nullable=False),
        sa.Column("promotion_outcome", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_agentic_promotion_decisions_series_id"),
        "agentic_promotion_decisions",
        ["series_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agentic_promotion_decisions_book_number"),
        "agentic_promotion_decisions",
        ["book_number"],
        unique=False,
    )
    op.create_index(
        "ix_agentic_promotion_decisions_series_id_book_number",
        "agentic_promotion_decisions",
        ["series_id", "book_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agentic_promotion_decisions_series_id_book_number", table_name="agentic_promotion_decisions"
    )
    op.drop_index(op.f("ix_agentic_promotion_decisions_book_number"), table_name="agentic_promotion_decisions")
    op.drop_index(op.f("ix_agentic_promotion_decisions_series_id"), table_name="agentic_promotion_decisions")
    op.drop_table("agentic_promotion_decisions")
