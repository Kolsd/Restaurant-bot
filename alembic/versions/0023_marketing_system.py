"""Marketing system: plan column, marketing_enabled flag, marketing_messages_log table.

Adds:
  - restaurants.plan TEXT NOT NULL DEFAULT 'basic'
  - restaurants.marketing_enabled BOOLEAN NOT NULL DEFAULT TRUE
  - marketing_messages_log table with cost tracking, status, and indexes

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-13
"""
from alembic import op

revision      = "0023"
down_revision = "0022"
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # 1. Add plan column to restaurants (IF NOT EXISTS for Railway safety)
    op.execute("""
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'basic'
    """)

    # 2. Add marketing_enabled flag to restaurants
    op.execute("""
        ALTER TABLE restaurants
            ADD COLUMN IF NOT EXISTS marketing_enabled BOOLEAN NOT NULL DEFAULT TRUE
    """)

    # 3. Create marketing_messages_log table
    op.execute("""
        CREATE TABLE IF NOT EXISTS marketing_messages_log (
            id                BIGSERIAL PRIMARY KEY,
            restaurant_id     INT NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
            customer_phone    TEXT NOT NULL,
            message_type      TEXT NOT NULL,
            template_name     TEXT,
            message_body      TEXT,
            sent_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            cost_estimate_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
            status            TEXT NOT NULL,
            error             TEXT,
            meta_message_id   TEXT
        )
    """)

    # 4. Index for recent history queries (restaurant + time DESC)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_marketing_log_restaurant_sent
            ON marketing_messages_log (restaurant_id, sent_at DESC)
    """)

    # 5. Index for count-by-month queries (restaurant + status + time)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_marketing_log_status_sent
            ON marketing_messages_log (restaurant_id, status, sent_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_marketing_log_status_sent")
    op.execute("DROP INDEX IF EXISTS ix_marketing_log_restaurant_sent")
    op.execute("DROP TABLE IF EXISTS marketing_messages_log")
    op.execute("ALTER TABLE restaurants DROP COLUMN IF EXISTS marketing_enabled")
    op.execute("ALTER TABLE restaurants DROP COLUMN IF EXISTS plan")
