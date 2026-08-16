"""add profiles table and profile_id columns

Adds multi-profile support: a `profiles` table (isolated libraries under
the single existing login -- see routers/deps.py) and a `profile_id`
column on `series`/`books`, seeded with two profiles ("robbie",
"daughter") and backfilled so every pre-existing row becomes Robbie's
library.

Deliberately a new table rather than reusing/renaming the existing
`users` table and its dormant `owner_id` columns (added in
f73cc32abaee): `users` stays reserved for real future per-account logins,
and `profiles.owner_user_id` (unused/unenforced for now, mirroring
`owner_id`) is the eventual attachment point between the two.

Column added nullable first, backfilled, then altered to NOT NULL --
altering straight to NOT NULL in one step would fail outright against a
database with existing rows.

Revision ID: a4f27c81de93
Revises: 8ab6ff881291
Create Date: 2026-08-15 22:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a4f27c81de93'
down_revision: Union[str, Sequence[str], None] = '8ab6ff881291'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_PROFILE_ID = "robbie"
DEFAULT_PROFILE_NAME = "Robbie's Library"
SECOND_PROFILE_ID = "daughter"
SECOND_PROFILE_NAME = "Daughter's Library"


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
    )

    bind.execute(
        sa.text(
            "INSERT INTO profiles (id, display_name, created_at, is_default) "
            "VALUES (:id, :display_name, CURRENT_TIMESTAMP, 1)"
        ),
        {"id": DEFAULT_PROFILE_ID, "display_name": DEFAULT_PROFILE_NAME},
    )
    bind.execute(
        sa.text(
            "INSERT INTO profiles (id, display_name, created_at, is_default) "
            "VALUES (:id, :display_name, CURRENT_TIMESTAMP, 0)"
        ),
        {"id": SECOND_PROFILE_ID, "display_name": SECOND_PROFILE_NAME},
    )

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_id", sa.String(), nullable=True))
        batch_op.create_index(batch_op.f("ix_series_profile_id"), ["profile_id"], unique=False)
        batch_op.create_foreign_key("fk_series_profile_id_profiles", "profiles", ["profile_id"], ["id"])

    with op.batch_alter_table("books", schema=None) as batch_op:
        batch_op.add_column(sa.Column("profile_id", sa.String(), nullable=True))
        batch_op.create_index(batch_op.f("ix_books_profile_id"), ["profile_id"], unique=False)
        batch_op.create_foreign_key("fk_books_profile_id_profiles", "profiles", ["profile_id"], ["id"])

    # Backfill: every pre-existing row becomes the default profile's data.
    bind.execute(sa.text("UPDATE series SET profile_id = :pid WHERE profile_id IS NULL"), {"pid": DEFAULT_PROFILE_ID})
    bind.execute(sa.text("UPDATE books SET profile_id = :pid WHERE profile_id IS NULL"), {"pid": DEFAULT_PROFILE_ID})

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.alter_column("profile_id", existing_type=sa.String(), nullable=False)
        # Query performance for crud.get_series_by_name's now-per-profile
        # lookup -- not a uniqueness constraint (existing data isn't
        # guaranteed unique per name today, and this migration shouldn't
        # be the place that starts rejecting rows over it).
        batch_op.create_index("ix_series_profile_id_name", ["profile_id", "name"], unique=False)

    with op.batch_alter_table("books", schema=None) as batch_op:
        batch_op.alter_column("profile_id", existing_type=sa.String(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("books", schema=None) as batch_op:
        batch_op.drop_constraint("fk_books_profile_id_profiles", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_books_profile_id"))
        batch_op.drop_column("profile_id")

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.drop_index("ix_series_profile_id_name")
        batch_op.drop_constraint("fk_series_profile_id_profiles", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_series_profile_id"))
        batch_op.drop_column("profile_id")

    op.drop_table("profiles")
