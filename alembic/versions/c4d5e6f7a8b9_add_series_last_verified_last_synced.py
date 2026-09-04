"""add last_verified_at and last_synced_at to series

Two-Timestamp UI Adjustments spec (locked 2026-09-04): adds two new,
narrower Series timestamps alongside the existing last_checked column
(which is left completely untouched -- see models.Series's own comment
on all three columns for the full rationale):

  - last_verified_at: stamped every time the user clicks "Search Book
    Online" on a series' detail page (POST /series/{id}/verify) -- a
    manual, best-effort audit stamp, independent of whether they actually
    found anything.
  - last_synced_at: stamped only when a "Check for New" run actually
    persists new book(s) to the series (services/series_check_engine.py's
    response_status == "success" branch) -- unlike last_checked, this
    stays NULL/unchanged on a run that completes but finds nothing.

Both are nullable with no backfill -- every existing series simply reads
as "never verified" / "never synced" until the corresponding action next
happens for it.

Revision ID: c4d5e6f7a8b9
Revises: a1b2c3d4e5f6
Create Date: 2026-09-04 11:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_verified_at', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('last_synced_at', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.drop_column('last_synced_at')
        batch_op.drop_column('last_verified_at')
