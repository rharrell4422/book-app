"""add canonical_title and metadata provenance columns to books

Adds the schema for the Add Book metadata intake redesign (see project
design chat): canonical_title (provider-resolved title, separate from the
user's always-preserved `title`), metadata_source (where title/author/isbn13
came from -- user/provider/import/discovery/NULL), book_number_source
(where book_number came from -- user/provider/title_inferred/NULL), and
needs_reresolution (flags a provider-sourced bind that was only
low-confidence, so it's verified but should be re-checked later). All four
are nullable and additive -- existing rows backfill to NULL, no existing
column changes type or meaning.

Note: autogenerate also flagged unrelated pre-existing drift (the
long-orphaned `series_canonical_entries` table, nullable/type diffs on a few
`series` columns, an index rename) -- left out on purpose to keep this
scoped to the four new columns, matching the precedent set in 8ab6ff881291.

Revision ID: 54adead73a62
Revises: 7ff0897d2bc0
Create Date: 2026-08-20 11:29:57.057556

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '54adead73a62'
down_revision: Union[str, Sequence[str], None] = '7ff0897d2bc0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.add_column(sa.Column('canonical_title', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('metadata_source', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('book_number_source', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('needs_reresolution', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('needs_reresolution')
        batch_op.drop_column('book_number_source')
        batch_op.drop_column('metadata_source')
        batch_op.drop_column('canonical_title')
