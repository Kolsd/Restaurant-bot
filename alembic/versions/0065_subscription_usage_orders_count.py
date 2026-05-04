"""Add orders_count column to subscription_usage for monthly order limit enforcement.

WHY:
  The original 0020_missing_runtime_tables migration created subscription_usage
  with total_tokens + total_invoices, supporting daily token + daily invoice
  enforcement. Plan limits also need a MONTHLY orders cap (a higher-frequency
  signal than tokens) so we add a per-day counter that we aggregate over the
  current month at read time.

NEW COLUMN:
  orders_count INTEGER NOT NULL DEFAULT 0

The column is added with IF NOT EXISTS so this migration is idempotent and
safe to re-run on partially-migrated databases.

Revision ID: 0065_subscription_usage_orders_count
Revises: 0064_orders_eta
Create Date: 2026-05-04
"""

from alembic import op


revision = "0065_sub_usage_orders"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE subscription_usage
        ADD COLUMN IF NOT EXISTS orders_count INTEGER NOT NULL DEFAULT 0
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE subscription_usage DROP COLUMN IF EXISTS orders_count")
