"""Customer profiles table for bot memory (Feature 2, Phase A).

Stores per-phone customer context scoped to a restaurant:
preferences, order history summary, and visit stats.

Revision ID: 0021
Revises: 0020
Create Date: 2026-04-13
"""
from alembic import op

revision      = "0021"
down_revision = "0020"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS customer_profiles (
            id                 BIGSERIAL    PRIMARY KEY,
            restaurant_id      INTEGER      NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            phone              TEXT         NOT NULL,
            display_name       TEXT,
            preferences        JSONB        NOT NULL DEFAULT '{}'::jsonb,
            last_order_summary TEXT,
            total_orders       INTEGER      NOT NULL DEFAULT 0,
            total_spent        NUMERIC(12, 2) NOT NULL DEFAULT 0,
            first_seen         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
            last_seen          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        )
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_profiles_restaurant_phone
            ON customer_profiles (restaurant_id, phone)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_customer_profiles_last_seen
            ON customer_profiles (restaurant_id, last_seen DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_customer_profiles_last_seen")
    op.execute("DROP INDEX IF EXISTS ux_customer_profiles_restaurant_phone")
    op.execute("DROP TABLE IF EXISTS customer_profiles")
