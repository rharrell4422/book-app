"""add book_titles_json column to notifications

Part of the "Durable Notifications: Count Fix + Title List + Dedupe"
follow-up spec: durable series-level discovery notifications
(kind="series_discovery_delta") now carry a compact, deduped list of the
book titles (with status tags) that contributed to that run's
count_new_books, alongside the existing aggregate count. See
models.Notification's docstring for the exact shape and dedupe semantics.

Revision ID: b6d2e5a9c1f3
Revises: e3a9f1c7b2d4
Create Date: 2026-08-30 15:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b6d2e5a9c1f3'
down_revision: Union[str, Sequence[str], None] = 'e3a9f1c7b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('book_titles_json', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('book_titles_json')
