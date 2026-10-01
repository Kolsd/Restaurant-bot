"""Diner memory: a restaurant remembers a diner's phone across visits.

A web diner gets a new `web:<uuid>` identity on every visit, so the
customer_profiles row the bot already reads never matched twice. With the
diner's consent, a profile is now keyed by a hash of a per-browser secret
(`customer_profiles.phone = 'device:<sha256>'`) and every diner session from
that browser points at it. History is computed from the orders of the linked
sessions; nothing is copied.

- customer_profiles.consent_at: when the diner said "recuérdame" (habeas data).
- customer_profiles.contact_phone: the phone the diner typed at checkout, for
  the restaurant's records only. It never unlocks the history elsewhere.
- diner_sessions.customer_profile_id: SET NULL when the diner says
  "olvidarme" and the profile is deleted.

Both tables already have RLS ENABLE+FORCE (org_isolation).

Revision ID: 0104_diner_memory
Revises:     0103_users_display_name
Create Date: 2026-10-01
"""
from alembic import op

revision = "0104_diner_memory"
down_revision = "0103_users_display_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE customer_profiles ADD COLUMN IF NOT EXISTS consent_at TIMESTAMPTZ")
    op.execute("ALTER TABLE customer_profiles ADD COLUMN IF NOT EXISTS contact_phone TEXT")
    op.execute(
        "ALTER TABLE diner_sessions ADD COLUMN IF NOT EXISTS customer_profile_id BIGINT "
        "REFERENCES customer_profiles(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_diner_sessions_customer_profile "
        "ON diner_sessions (customer_profile_id) WHERE customer_profile_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_diner_sessions_customer_profile")
    op.execute("ALTER TABLE diner_sessions DROP COLUMN IF EXISTS customer_profile_id")
    op.execute("ALTER TABLE customer_profiles DROP COLUMN IF EXISTS contact_phone")
    op.execute("ALTER TABLE customer_profiles DROP COLUMN IF EXISTS consent_at")
