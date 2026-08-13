"""add source_url column to books

Stores the retailer/catalog page a book was discovered from (if any), so
the UI can offer a "check online" link for the user to verify details
themselves (e.g. an unconfirmed release date on a brand-new preorder)
rather than the app scraping retailer pages to extract that data.

Note: autogenerate also flagged unrelated pre-existing drift (the
long-orphaned `series_canonical_entries` table, nullable/type diffs on a
few `series` columns) -- left out on purpose to keep this scoped to the
source_url change, matching the precedent set in f73cc32abaee.

Revision ID: 8ab6ff881291
Revises: f73cc32abaee
Create Date: 2026-08-13 13:55:18.783732

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '8ab6ff881291'
down_revision: Union[str, Sequence[str], None] = 'f73cc32abaee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_url', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('source_url')
