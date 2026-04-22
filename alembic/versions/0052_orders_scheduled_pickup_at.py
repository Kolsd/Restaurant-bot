"""
0052 — Add orders.scheduled_pickup_at TIMESTAMPTZ.

Captures the customer's requested pickup time so kitchen staff can prioritise
preparation. NULL means no time was specified (non-pickup or unspecified).

Revision ID: 0052
Revises: 0051
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE orders
        ADD COLUMN IF NOT EXISTS scheduled_pickup_at TIMESTAMPTZ DEFAULT NULL
    """)

    # Index for caja/kitchen: find upcoming pickup orders sorted by scheduled time
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_orders_scheduled_pickup_at
        ON orders (scheduled_pickup_at)
        WHERE scheduled_pickup_at IS NOT NULL
          AND order_type = 'recoger'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orders_scheduled_pickup_at")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS scheduled_pickup_at")
