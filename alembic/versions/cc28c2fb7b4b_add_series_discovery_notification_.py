"""add series-level discovery notification fields, retire legacy rows

Evolves the existing `notifications` table (added in d72f86150e45 for the
old per-book "New Books Added to Library" popup) into the durable
series-level discovery notification design: adds `count_new_books` and
`series_name`, and retires every pre-existing undismissed row (all of
which are the old kind="new_book" shape, incompatible with the new
per-series display) by stamping `dismissed_at = now()` on them. The new
read endpoint additionally filters by kind="series_discovery_delta" as
defense-in-depth, so this backfill isn't the only thing keeping legacy
rows out of the new Notifications view -- but it's still done here so the
view starts clean rather than depending solely on that filter forever.

Revision ID: cc28c2fb7b4b
Revises: d72f86150e45
Create Date: 2026-08-21 10:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'cc28c2fb7b4b'
down_revision: Union[str, Sequence[str], None] = 'd72f86150e45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('count_new_books', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('series_name', sa.String(), nullable=True))

    # Retire every legacy (pre-aggregation) row still marked unseen -- see
    # module docstring. Irreversible by design; downgrade() below does not
    # attempt to restore prior dismissed_at values.
    op.execute("UPDATE notifications SET dismissed_at = CURRENT_TIMESTAMP WHERE dismissed_at IS NULL")


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('series_name')
        batch_op.drop_column('count_new_books')
