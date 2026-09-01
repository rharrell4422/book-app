"""add series_fingerprint table

Series Fingerprint system (see discovery_agentic_fingerprint_recommendation.md
for the full ten-round design chain): a new `series_fingerprint` table
giving discovery a durable, per-series *identity/pattern* memory --
author aliases, catalog naming-noise patterns, per-provider trust bias,
and release-cadence statistics -- as a narrow, additive companion to the
existing `series_skeleton` table (which stays the sole owner of titles,
numbering, status, and gaps; see models.SeriesFingerprint's docstring for
the exact boundary).

This migration only creates the table. It is not read or wired into any
scoring path until `settings.FINGERPRINT_INFLUENCE_ENABLED` and a
per-series `settings.FINGERPRINT_SERIES_ACTIVATION` entry are both set --
see services/fingerprint_store.py.

Revision ID: d4e8f2a91c37
Revises: c8e2a4f6b1d9
Create Date: 2026-09-01 10:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'd4e8f2a91c37'
down_revision: Union[str, Sequence[str], None] = 'c8e2a4f6b1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "series_fingerprint",
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("fingerprint_json", sqlite.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("series_id"),
    )


def downgrade() -> None:
    op.drop_table("series_fingerprint")
