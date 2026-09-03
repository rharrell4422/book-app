"""add guided discovery fields to series (canonical_url/canonical_source/verified_volume_count)

Guided Discovery architecture (locked 2026-09-03, iterations 1-5): adds
three optional, user-supplied series-level fields --

  - canonical_url: the URL the user actually sources this series from
    (e.g. its Kindle Unlimited catalog page on Amazon).
  - canonical_source: which storefront/source that URL is (KU, Nook,
    Kobo, GooglePlay, PublisherSite, Goodreads, Other) -- free-form
    string at the DB layer, validated at the Pydantic layer instead of a
    DB enum, so adding a new source later never needs a migration.
  - verified_volume_count: how many volumes the user has personally
    verified exist there.

All three are nullable and have no backfill -- per the locked "retroactive
gating" decision, Guided Discovery applies only to newly created series;
every existing series keeps working completely unaffected with these left
NULL (see discovery_engine._reconstruct_series_skeleton's own docstring
for how a NULL verified_volume_count reproduces pre-Guided-Discovery
behavior exactly).

Revision ID: a1b2c3d4e5f6
Revises: b3f7a29e6d41
Create Date: 2026-09-03 08:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'b3f7a29e6d41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.add_column(sa.Column('canonical_url', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('canonical_source', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('verified_volume_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('series', schema=None) as batch_op:
        batch_op.drop_column('verified_volume_count')
        batch_op.drop_column('canonical_source')
        batch_op.drop_column('canonical_url')
