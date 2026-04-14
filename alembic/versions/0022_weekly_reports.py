"""Weekly owner reports table + owner_phone / timezone columns on restaurants.

Feature 4 — Round 1 (Schema + Repo): data layer only.
Scheduler tick and dashboard UI come in later rounds.

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-13
"""
from alembic import op

revision      = "0022"
down_revision = "0021"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # 1. Nullable columns on restaurants — existing rows keep NULL / default.
    op.execute("""
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS owner_phone TEXT
    """)
    op.execute("""
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS timezone TEXT DEFAULT 'America/Bogota'
    """)

    # 2. Main table for weekly report records.
    op.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id               BIGSERIAL    PRIMARY KEY,
            restaurant_id    INTEGER      NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            week_start       DATE         NOT NULL,
            generated_at     TIMESTAMPTZ  DEFAULT NOW(),
            sent_at          TIMESTAMPTZ,
            payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
            message_text     TEXT,
            owner_phone      TEXT,
            delivery_status  TEXT         DEFAULT 'pending',
            error_message    TEXT,
            UNIQUE(restaurant_id, week_start)
        )
    """)

    # 3. Index for dashboard and scheduler lookups (most-recent-first per restaurant).
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_weekly_reports_restaurant_week
            ON weekly_reports (restaurant_id, week_start DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_weekly_reports_restaurant_week")
    op.execute("DROP TABLE IF EXISTS weekly_reports")
    op.execute("ALTER TABLE restaurants DROP COLUMN IF EXISTS timezone")
    op.execute("ALTER TABLE restaurants DROP COLUMN IF EXISTS owner_phone")
