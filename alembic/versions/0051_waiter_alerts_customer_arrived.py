"""
0051 — Document customer_arrived alert_type convention.

waiter_alerts.alert_type is TEXT with no CHECK constraint (confirmed by schema
inspection: no pg_constraint on that column in 0001_initial_schema.py).
This migration adds a comment on the column so the convention is visible at
the DB level and adds an index for fast lookup of customer_arrived alerts by
phone + bot_number.

Revision ID: 0051
Revises: 0050
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0051"
down_revision = "0050_orders_cancel"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The alert_type column is TEXT — no enum or check constraint to alter.
    # Add a comment so the convention is documented at DB level.
    op.execute("""
        COMMENT ON COLUMN waiter_alerts.alert_type IS
            'Known values: waiter, bill, customer_arrived. TEXT — no enum; add new values here.'
    """)

    # Index for fast lookup: caja polling for customer_arrived alerts by bot_number
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_waiter_alerts_customer_arrived
        ON waiter_alerts (bot_number, alert_type, dismissed)
        WHERE alert_type = 'customer_arrived'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_waiter_alerts_customer_arrived")
    # Column comment downgrade is a no-op (just removes the comment)
    op.execute("COMMENT ON COLUMN waiter_alerts.alert_type IS NULL")
