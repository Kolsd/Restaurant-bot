"""
0062 — loyalty discount columns on orders and table_checks.

Background:
  The bot now exposes loyalty point redemption as a tool to the customer.
  When the customer redeems N points, db_redeem_loyalty_points decrements
  loyalty_customers.points_balance and writes a 'redeem' ledger entry. But
  caja must also see the discount on the actual order/check it is going to
  cobrar — otherwise the customer redeems points on WhatsApp and caja still
  charges them the full amount.

  This migration adds:
    - orders.loyalty_redeemed_points  (delivery / pickup orders)
    - orders.loyalty_discount_cop
    - table_checks.loyalty_redeemed_points  (dine-in checks)
    - table_checks.loyalty_discount_cop

  Both default to 0 NOT NULL so existing rows have a valid baseline and the
  caja UI can read these fields unconditionally.

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-03
"""

from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE orders "
        "ADD COLUMN IF NOT EXISTS loyalty_redeemed_points INTEGER DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE orders "
        "ADD COLUMN IF NOT EXISTS loyalty_discount_cop NUMERIC(14,2) DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE table_checks "
        "ADD COLUMN IF NOT EXISTS loyalty_redeemed_points INTEGER DEFAULT 0 NOT NULL"
    )
    op.execute(
        "ALTER TABLE table_checks "
        "ADD COLUMN IF NOT EXISTS loyalty_discount_cop NUMERIC(14,2) DEFAULT 0 NOT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE orders        DROP COLUMN IF EXISTS loyalty_redeemed_points")
    op.execute("ALTER TABLE orders        DROP COLUMN IF EXISTS loyalty_discount_cop")
    op.execute("ALTER TABLE table_checks  DROP COLUMN IF EXISTS loyalty_redeemed_points")
    op.execute("ALTER TABLE table_checks  DROP COLUMN IF EXISTS loyalty_discount_cop")
