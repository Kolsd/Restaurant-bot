"""Add cancelled_at + cancelled_reason to orders table (customer self-cancellation support).

Revision ID: 0050_orders_cancel
Revises: 0049_loyalty_camps
Create Date: 2026-04-21
"""

from alembic import op

revision = "0050_orders_cancel"
down_revision = "0049_loyalty_camps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS cancelled_at      TIMESTAMPTZ DEFAULT NULL,
            ADD COLUMN IF NOT EXISTS cancelled_reason  TEXT        DEFAULT NULL
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE orders
            DROP COLUMN IF EXISTS cancelled_at,
            DROP COLUMN IF EXISTS cancelled_reason
    """)
