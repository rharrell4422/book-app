"""add users table and owner_id columns

Schema-only multi-tenancy groundwork: adds a `users` table and a nullable,
unenforced `owner_id` FK on `series`/`books`, then seeds a single "owner"
user and backfills every existing row to point at it. Nothing reads
owner_id yet (auth is still the single-owner-password + share-token scheme
in routers/deps.py) -- this just means a future move to real per-user
accounts is a data/auth migration, not also a schema-design exercise.

Note: autogenerate also flagged unrelated drift (the long-orphaned,
unreferenced `series_canonical_entries` table, and a couple of nullable/type
diffs on `series` columns that predate Alembic) -- left out of this revision
on purpose to keep it scoped to the owner_id change; can be its own cleanup
migration later.

Revision ID: f73cc32abaee
Revises: bf8439427a1e
Create Date: 2026-07-13 10:23:05.049112

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f73cc32abaee'
down_revision: Union[str, Sequence[str], None] = 'bf8439427a1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OWNER_EMAIL = "owner@local"


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_id"), ["id"], unique=False)

    bind.execute(
        sa.text("INSERT INTO users (email, display_name, role) VALUES (:email, :display_name, :role)"),
        {"email": OWNER_EMAIL, "display_name": "Owner", "role": "owner"},
    )
    owner_id = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email"), {"email": OWNER_EMAIL}
    ).scalar_one()

    with op.batch_alter_table("books", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_books_owner_id"), ["owner_id"], unique=False)
        batch_op.create_foreign_key("fk_books_owner_id_users", "users", ["owner_id"], ["id"])

    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_series_owner_id"), ["owner_id"], unique=False)
        batch_op.create_foreign_key("fk_series_owner_id_users", "users", ["owner_id"], ["id"])

    # Backfill: every pre-existing row belongs to the single owner seeded above.
    bind.execute(sa.text("UPDATE books SET owner_id = :owner_id WHERE owner_id IS NULL"), {"owner_id": owner_id})
    bind.execute(sa.text("UPDATE series SET owner_id = :owner_id WHERE owner_id IS NULL"), {"owner_id": owner_id})


def downgrade() -> None:
    with op.batch_alter_table("series", schema=None) as batch_op:
        batch_op.drop_constraint("fk_series_owner_id_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_series_owner_id"))
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("books", schema=None) as batch_op:
        batch_op.drop_constraint("fk_books_owner_id_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_books_owner_id"))
        batch_op.drop_column("owner_id")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_id"))

    op.drop_table("users")
