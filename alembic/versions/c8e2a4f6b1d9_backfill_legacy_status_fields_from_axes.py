"""backfill legacy read_status/is_upcoming_* fields for blank/non-canonical rows

Part of the "Two-Axis Status Architecture" design chat's finalized Phase-3
decision. The frontend's unified status badge (see book-app-ui/lib/
book-format.ts's getUnifiedBookStatus) now reads is_read + availability_status
directly instead of the legacy read_status bridge string -- but a handful of
rows in the real database were found to have a blank read_status (contaminated
by an earlier session's migration/verification step that wrote directly to
the real DB instead of a test DB; see availability_status/is_read on those
rows, which are correctly populated). intelligence/core.py's series-aggregate
functions still read the legacy read_status field, so those rows are a live
latent bug there independent of the frontend change.

This is a pure data-hygiene backfill, not a schema change: for any row whose
read_status is blank or doesn't match one of the four canonical values, it
re-derives read_status/is_upcoming_auto/is_upcoming_final from that row's own
authoritative is_read/availability_status/availability_locked, using the exact
same forward-derivation table as services.availability_bridge.
derive_legacy_fields (duplicated here as raw SQL, since Alembic migrations in
this project don't import application modules -- see f1a2b3c4d5e6's equivalent
raw-SQL backfill for the established pattern). Deliberately scoped to "blank
or non-canonical" rather than hardcoded row ids, so it also self-heals any
future row that reaches this state via a write path that bypasses the bridge.

Revision ID: c8e2a4f6b1d9
Revises: f1a2b3c4d5e6
Create Date: 2026-08-31 18:35:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c8e2a4f6b1d9'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BLANK_OR_NONCANONICAL_WHERE = """
    read_status IS NULL
    OR trim(read_status) = ''
    OR lower(trim(read_status)) NOT IN ('read', 'unread', 'available', 'upcoming')
"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE books
        SET
            read_status = CASE
                WHEN is_read = 1 THEN 'read'
                WHEN lower(trim(coalesce(availability_status, ''))) = 'owned' THEN 'unread'
                WHEN lower(trim(coalesce(availability_status, ''))) = 'upcoming' THEN 'upcoming'
                ELSE 'available'
            END,
            is_upcoming_auto = CASE
                WHEN lower(trim(coalesce(availability_status, ''))) = 'upcoming'
                     AND coalesce(availability_locked, 0) = 0 THEN 1
                ELSE 0
            END,
            is_upcoming_final = CASE
                WHEN lower(trim(coalesce(availability_status, ''))) = 'upcoming'
                     AND coalesce(availability_locked, 0) = 1 THEN 1
                ELSE 0
            END
        WHERE {_BLANK_OR_NONCANONICAL_WHERE}
        """
    )


def downgrade() -> None:
    # No-op: this is a data-hygiene fix for rows that were already broken
    # (blank/non-canonical read_status), not a reversible schema or
    # behavior change -- there's nothing meaningful to revert to.
    pass
