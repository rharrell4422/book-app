"""add tier_c_promotion_history table, is_manual_override flag, and
duration_ms/tier_c_state_at_call columns on shadow_llm_calls

Step 9 (Tier C Promotion Policy Engine): turns Step 8's manual-only
tier_c_promotion_state into an automated, auditable per-series policy
mechanism.

- shadow_llm_calls.duration_ms / .tier_c_state_at_call: two columns the
  policy engine needs that Step 8 didn't persist. `duration_ms` was
  already computed at the call site (agents/series_agent.py) and handed
  to the per-run in-memory telemetry, just never threaded into this
  durable row. `tier_c_state_at_call` snapshots which tier_c_state was
  active *at the moment of this call* -- without it, a disagreement row
  read later can't be classified as "a live override" vs. "just a shadow
  disagreement", since tier_c_state can change between calls.

- tier_c_promotion_state.is_manual_override: an admin-set freeze flag.
  Once TierCPromotionPolicyEngine starts writing this table automatically
  every Check Now job, a manual admin write needs a way to survive the
  next automated evaluation -- see services/tier_c_promotion_engine.py's
  module docstring. Set via direct DB write, same as tier_c_state itself
  today (no admin endpoint yet -- explicit future work, matching Step 8's
  own precedent).

- tier_c_promotion_history: one row per policy evaluation (one per
  series per Check Now job), modeled after shadow_llm_calls' fail-soft,
  independent-session dual-write pattern. Records the transition (or
  HOLD), why, and the metrics snapshot the decision was based on --
  auditability for a mechanism that now moves tier_c_state without a
  human in the loop.

Revision ID: 0cb8c7f90204
Revises: f4b7c92e1a08
Create Date: 2026-09-02 09:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0cb8c7f90204'
down_revision: Union[str, Sequence[str], None] = 'f4b7c92e1a08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_llm_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("duration_ms", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("tier_c_state_at_call", sa.String(), nullable=True))

    with op.batch_alter_table("tier_c_promotion_state", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_manual_override", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.create_table(
        "tier_c_promotion_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.Column("previous_state", sa.String(), nullable=False),
        sa.Column("new_state", sa.String(), nullable=False),
        sa.Column("evaluation_reason", sa.String(), nullable=False),
        sa.Column("shadow_calls_considered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("agreement_rate", sa.Float(), nullable=True),
        sa.Column("manual_override_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metrics_snapshot", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tier_c_promotion_history_series_id"), "tier_c_promotion_history", ["series_id"], unique=False
    )
    op.create_index(
        op.f("ix_tier_c_promotion_history_evaluated_at"), "tier_c_promotion_history", ["evaluated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tier_c_promotion_history_evaluated_at"), table_name="tier_c_promotion_history")
    op.drop_index(op.f("ix_tier_c_promotion_history_series_id"), table_name="tier_c_promotion_history")
    op.drop_table("tier_c_promotion_history")

    with op.batch_alter_table("tier_c_promotion_state", schema=None) as batch_op:
        batch_op.drop_column("is_manual_override")

    with op.batch_alter_table("shadow_llm_calls", schema=None) as batch_op:
        batch_op.drop_column("tier_c_state_at_call")
        batch_op.drop_column("duration_ms")
