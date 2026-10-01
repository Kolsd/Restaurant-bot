"""users.display_name: what the owner is called, not just how they log in.

Self-serve signup asks for the owner's name ("María García") but only ever
filed it in the CRM; the account itself kept the email as its login and the
dashboard greeted the owner with it ("Buenas tardes, admin@gmail.com"). The
name now lives on the user row and the login response returns it.
Nullable: accounts that already exist have none and keep working.

Revision ID: 0103_users_display_name
Revises:     0102_paid_until
Create Date: 2026-10-01
"""
from alembic import op

revision = "0103_users_display_name"
down_revision = "0102_paid_until"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS display_name")
