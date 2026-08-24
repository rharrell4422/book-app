"""add version column to series_skeleton

CR-4: `series_skeleton` has two independent write call sites that can
legitimately race (a boot-time backfill sweep vs. an in-flight Check Now
job's post-persistence apply). The existing upsert-with-retry
(services/skeleton_store.py's `_upsert_skeleton_row`) already protects
against a concurrent-INSERT race via the `series_id` primary key's
IntegrityError, but two concurrent successful UPDATEs had no optimistic-
version check at all -- both could commit, with the second silently
clobbering the first's merge_fn result computed from an already-stale
read.

This migration only adds the column (nullable=False, backfilled to 0 via
server_default for every existing row). `_upsert_skeleton_row` is updated
in the same change to read-then-conditionally-write on this value,
incrementing it by 1 on every successful write and retrying (same as the
existing IntegrityError/OperationalError path) when the conditional
UPDATE affects zero rows.

Revision ID: a1a17b22f53a
Revises: cc28c2fb7b4b
Create Date: 2026-08-24 14:32:30.036896

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1a17b22f53a'
down_revision: Union[str, Sequence[str], None] = 'cc28c2fb7b4b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('series_skeleton', schema=None) as batch_op:
        batch_op.add_column(sa.Column('version', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('series_skeleton', schema=None) as batch_op:
        batch_op.drop_column('version')
