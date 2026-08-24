"""bump series_skeleton schema_version default to 2

FIX-SS-ENUM (part 1): `services/skeleton_store.SCHEMA_VERSION` has been 2
since the `source_class` field was added (both real write paths --
`_upsert_skeleton_row`'s insert and update branches -- already explicitly
set `schema_version=SCHEMA_VERSION` on every write), but
`models.SeriesSkeleton.schema_version`'s column-level default was left at
the stale `1`, and the DB column itself has never had a server_default at
all. Neither has ever caused a real bug -- nothing in the codebase reads
`SeriesSkeleton.schema_version` to branch on (confirmed via a repo-wide
search), and every actual write path sets it explicitly -- but the stale
default is misleading and this closes the gap for defense-in-depth: any
future write path that omits the column now gets `2`, matching reality,
instead of silently regressing to a value that hasn't described the real
row shape since `source_class` was introduced.

This migration (a) sets a `server_default` of `'2'` on the column via
`batch_alter_table` (mirroring CR-4's `a1a17b22f53a` pattern) and (b)
backfills any existing row still sitting at the stale `1` to `2`. This
backfill is safe unconditionally -- it only touches the row-level
`schema_version` marker, never `skeleton_json` itself, and (as above)
nothing branches on this column's value today.

Revision ID: 5414264c11af
Revises: a1a17b22f53a
Create Date: 2026-08-24 16:23:35.812833

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5414264c11af'
down_revision: Union[str, Sequence[str], None] = 'a1a17b22f53a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('series_skeleton', schema=None) as batch_op:
        batch_op.alter_column(
            'schema_version',
            existing_type=sa.Integer(),
            nullable=False,
            server_default='2',
        )

    series_skeleton = sa.table('series_skeleton', sa.column('schema_version', sa.Integer()))
    op.execute(series_skeleton.update().where(series_skeleton.c.schema_version == 1).values(schema_version=2))


def downgrade() -> None:
    with op.batch_alter_table('series_skeleton', schema=None) as batch_op:
        batch_op.alter_column(
            'schema_version',
            existing_type=sa.Integer(),
            nullable=False,
            server_default='1',
        )
