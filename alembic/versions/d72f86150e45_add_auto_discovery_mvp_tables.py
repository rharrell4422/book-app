"""add auto discovery mvp: last_full_discovery_run_at + notifications table

Adds the schema for the Auto Discovery MVP spec (see project design chat):
Profile.last_full_discovery_run_at (rate-limit cooldown stamp for the "Full
Auto Discovery" button, §4) and a new `notifications` table (minimal "New
Books Added to Library" popup, §3). The Discovery Health Indicator (§1) adds
no schema -- it's a derived @property off the existing Series.last_checked
column (see models.Series.discovery_health) -- and the eligibility filter
(§2) is pure Python over existing columns, so neither needs a migration.

Revision ID: d72f86150e45
Revises: 54adead73a62
Create Date: 2026-08-20 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd72f86150e45'
down_revision: Union[str, Sequence[str], None] = '54adead73a62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_full_discovery_run_at', sa.DateTime(), nullable=True))

    op.create_table(
        'notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.String(), nullable=False),
        sa.Column('book_id', sa.Integer(), nullable=True),
        sa.Column('series_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('dismissed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['profiles.id']),
        sa.ForeignKeyConstraint(['book_id'], ['books.id']),
        sa.ForeignKeyConstraint(['series_id'], ['series.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_id'), ['id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_profile_id'), ['profile_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_created_at'), ['created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_created_at'))
        batch_op.drop_index(batch_op.f('ix_notifications_profile_id'))
        batch_op.drop_index(batch_op.f('ix_notifications_id'))
    op.drop_table('notifications')

    with op.batch_alter_table('profiles', schema=None) as batch_op:
        batch_op.drop_column('last_full_discovery_run_at')
