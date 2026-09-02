"""add shadow_llm_calls and tier_c_promotion_state tables, tier_c_disagreement column

Step 8 (Tier C Shadow Scoring Persistence + Promotion Path): turns Step 7's
per-run, in-memory-only Tier C shadow scoring (DiscoveryTelemetry.record_
tier_c_shadow_score) into a persisted, queryable signal.

- shadow_llm_calls: one durable row per Tier C shadow LLM call, storing
  the gate context, shadow output, and _score_tier_c_shadow_response's
  scoring outputs verbatim (never recomputed here) plus cost/token/meta.
  See models.ShadowLLMCall's docstring for the full field rationale.

- tier_c_promotion_state: current Tier C promotion state per series
  (shadow_only/shadow_advisory/live), one row per series, current-state
  only -- no transition history yet (explicit Phase 8b future work). A
  missing row means "shadow_only" (see models.TierCPromotionState and
  services/tier_c_shadow_store.get_tier_c_promotion_state).

- series_candidate_notifications.tier_c_disagreement: nullable JSON,
  populated only when a series' Tier C state is "shadow_advisory" and the
  Tier C shadow call for that same candidate disagreed with the
  deterministic gate (see models.SeriesCandidateNotification's docstring).

This migration only creates the tables/column. Nothing reads/writes them
in production until agents/series_agent.py's Tier C shadow call site
(Step 8's implementation) is deployed, and no series is ever moved out of
the implicit "shadow_only" default until a future admin endpoint or manual
DB write does so (Phase 8a ships no automated promotion policy).

Revision ID: f4b7c92e1a08
Revises: d4e8f2a91c37
Create Date: 2026-09-01 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f4b7c92e1a08'
down_revision: Union[str, Sequence[str], None] = 'd4e8f2a91c37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shadow_llm_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=False, server_default="C"),
        sa.Column("gate_belongs_to_series", sa.Boolean(), nullable=False),
        sa.Column("gate_inferred_number", sa.Integer(), nullable=True),
        sa.Column("gate_confidence", sa.String(), nullable=True),
        sa.Column("shadow_provider", sa.String(), nullable=False),
        sa.Column("shadow_model_id", sa.String(), nullable=False),
        sa.Column("shadow_belongs_to_series", sa.Boolean(), nullable=True),
        sa.Column("shadow_inferred_number", sa.Integer(), nullable=True),
        sa.Column("shadow_confidence", sa.String(), nullable=True),
        sa.Column("shadow_is_alternate_title_of_known_book", sa.Boolean(), nullable=True),
        sa.Column("parsed_ok", sa.Boolean(), nullable=False),
        sa.Column("belongs_to_series_agreement", sa.Boolean(), nullable=True),
        sa.Column("inferred_number_agreement", sa.Boolean(), nullable=True),
        sa.Column("confidence_aligned", sa.Boolean(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_shadow_llm_calls_series_id"), "shadow_llm_calls", ["series_id"], unique=False
    )
    op.create_index(op.f("ix_shadow_llm_calls_run_id"), "shadow_llm_calls", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_shadow_llm_calls_created_at"), "shadow_llm_calls", ["created_at"], unique=False
    )

    op.create_table(
        "tier_c_promotion_state",
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("tier_c_state", sa.String(), nullable=False, server_default="shadow_only"),
        sa.Column("tier_c_provider", sa.String(), nullable=True),
        sa.Column("tier_c_model_id", sa.String(), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("series_id"),
    )

    with op.batch_alter_table("series_candidate_notifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tier_c_disagreement", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("series_candidate_notifications", schema=None) as batch_op:
        batch_op.drop_column("tier_c_disagreement")

    op.drop_table("tier_c_promotion_state")

    op.drop_index(op.f("ix_shadow_llm_calls_created_at"), table_name="shadow_llm_calls")
    op.drop_index(op.f("ix_shadow_llm_calls_run_id"), table_name="shadow_llm_calls")
    op.drop_index(op.f("ix_shadow_llm_calls_series_id"), table_name="shadow_llm_calls")
    op.drop_table("shadow_llm_calls")
