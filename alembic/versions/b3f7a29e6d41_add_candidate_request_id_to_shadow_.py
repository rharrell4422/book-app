"""add candidate_request_id column to shadow_llm_calls

Step 10 Phase 1 (Multi-Provider Tier C, schema/settings scaffolding):
adds the per-candidate correlation key `shadow_llm_calls.
candidate_request_id` that a later phase's parallel Tier C fan-out
(Anthropic + Groq + OpenAI) will use to group multiple providers' rows
for the same candidate -- see models.ShadowLLMCall's docstring for the
full rationale (a per-invocation key, not a book-identity key).

This migration only adds the nullable column. Nothing mints or writes a
real value here -- `services.tier_c_shadow_store.persist_tier_c_shadow_
call` accepts it as an optional, defaulted-to-None kwarg (same "wire
ahead of use" sequencing as Step 9's `duration_ms`/`tier_c_state_at_call`
columns), and the Tier C shadow call site in `agents/series_agent.py`
does not pass one yet. No behavior change.

Revision ID: b3f7a29e6d41
Revises: 0cb8c7f90204
Create Date: 2026-09-02 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3f7a29e6d41'
down_revision: Union[str, Sequence[str], None] = '0cb8c7f90204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shadow_llm_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("candidate_request_id", sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_shadow_llm_calls_candidate_request_id"),
            ["candidate_request_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("shadow_llm_calls", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_shadow_llm_calls_candidate_request_id"))
        batch_op.drop_column("candidate_request_id")
