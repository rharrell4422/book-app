"""add series_candidate_notifications table

Adds the durable "Review Candidate Book" notification table for the
LitRPG Enhanced Discovery design chat's finalized spec: the
`low_confidence_ambiguous and (overall_grade in {"medium", None})` branch
in `agents/series_agent.py` (previously `needs_review.append(...)`) now
writes one of these rows per ambiguous candidate instead, and no longer
writes an unconfirmed SeriesSkeleton entry for it either -- see
models.SeriesCandidateNotification's docstring for the full rationale.

Revision ID: e3a9f1c7b2d4
Revises: 120718346871
Create Date: 2026-08-30 09:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e3a9f1c7b2d4'
down_revision: Union[str, Sequence[str], None] = '120718346871'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'series_candidate_notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('series_id', sa.Integer(), nullable=True),
        sa.Column('series_name', sa.String(), nullable=True),
        sa.Column('candidate_title', sa.String(), nullable=False),
        sa.Column('candidate_number', sa.Float(), nullable=True),
        sa.Column('overall_confidence', sa.String(), nullable=True),
        sa.Column('provider_confidence', sa.String(), nullable=True),
        sa.Column('isbn13', sa.String(), nullable=True),
        sa.Column('publication_date', sa.String(), nullable=True),
        sa.Column('asin', sa.String(), nullable=True),
        sa.Column('author', sa.String(), nullable=True),
        sa.Column('source_url', sa.String(), nullable=True),
        sa.Column('provider', sa.String(), nullable=True),
        sa.Column('series_name_hint', sa.String(), nullable=True),
        sa.Column('reason_flags', sa.JSON(), nullable=False),
        sa.Column('title_key', sa.String(), nullable=False),
        sa.Column('bare_title_key', sa.String(), nullable=False),
        sa.Column('resolution', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['profiles.id']),
        sa.ForeignKeyConstraint(['series_id'], ['series.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('series_candidate_notifications', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_id'), ['id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_profile_id'), ['profile_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_series_id'), ['series_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_title_key'), ['title_key'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_resolution'), ['resolution'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_series_candidate_notifications_created_at'), ['created_at'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('series_candidate_notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_created_at'))
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_resolution'))
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_title_key'))
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_series_id'))
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_profile_id'))
        batch_op.drop_index(batch_op.f('ix_series_candidate_notifications_id'))
    op.drop_table('series_candidate_notifications')
