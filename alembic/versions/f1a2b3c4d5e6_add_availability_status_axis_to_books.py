"""add availability_status/availability_locked two-axis columns to books

Part of the "Two-Axis Status Architecture" design chat's finalized spec:
introduces `availability_status` ("upcoming"/"available"/"owned") and
`availability_locked` (bool) as a second, independent axis alongside the
existing reading axis (`is_read`/`read_date`) -- see models.Book's own
docstring on these two columns for the full rationale.

Backfill runs in two explicit passes so ordering is deterministic rather
than left to chance:

  1. `is_upcoming_final=true` rows -> `availability_locked=true` +
     `availability_status="upcoming"`. This is the "confirmed/locked
     upcoming" signal that already existed pre-migration (see
     library_sync.py's `is_marked_upcoming` check) -- preserved exactly.
  2. Every remaining row (i.e. NOT already locked by pass 1) gets
     `availability_status` from its existing `read_status`: read->owned,
     available->available, upcoming->upcoming, unread->owned. Rows whose
     `read_status` doesn't match one of those four (blank/legacy/
     unrecognized) are left at the column default ("available") and
     unlocked, same as a freshly-created row.

`is_read` is never touched by this migration -- it already is, and
remains, the sole source of truth for reading progress.

Revision ID: f1a2b3c4d5e6
Revises: b6d2e5a9c1f3
Create Date: 2026-08-31 15:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'b6d2e5a9c1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.add_column(sa.Column('availability_status', sa.String(), nullable=True, server_default='available'))
        batch_op.add_column(sa.Column('availability_locked', sa.Boolean(), nullable=True, server_default=sa.false()))

    # Pass 1: is_upcoming_final=true is the pre-existing "locked upcoming"
    # signal -- preserve it exactly rather than re-deriving it from
    # read_status below, which lacks a locked/unlocked distinction of its
    # own.
    op.execute(
        """
        UPDATE books
        SET availability_status = 'upcoming', availability_locked = 1
        WHERE is_upcoming_final = 1
        """
    )

    # Pass 2: for every row pass 1 didn't already lock, derive
    # availability_status from the legacy read_status value. Rows with no
    # recognized read_status keep the column default ('available') and
    # stay unlocked -- exactly like a brand-new row.
    op.execute(
        """
        UPDATE books
        SET availability_status = 'owned'
        WHERE availability_locked IS NOT 1
          AND lower(trim(coalesce(read_status, ''))) = 'read'
        """
    )
    op.execute(
        """
        UPDATE books
        SET availability_status = 'available'
        WHERE availability_locked IS NOT 1
          AND lower(trim(coalesce(read_status, ''))) = 'available'
        """
    )
    op.execute(
        """
        UPDATE books
        SET availability_status = 'upcoming'
        WHERE availability_locked IS NOT 1
          AND lower(trim(coalesce(read_status, ''))) = 'upcoming'
        """
    )
    op.execute(
        """
        UPDATE books
        SET availability_status = 'owned'
        WHERE availability_locked IS NOT 1
          AND lower(trim(coalesce(read_status, ''))) = 'unread'
        """
    )
    op.execute("UPDATE books SET availability_status = 'available' WHERE availability_status IS NULL")
    op.execute("UPDATE books SET availability_locked = 0 WHERE availability_locked IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('availability_locked')
        batch_op.drop_column('availability_status')
