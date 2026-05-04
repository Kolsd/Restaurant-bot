"""
0064 — orders ETA: estimated_minutes + eta_communicated.

Adds two columns to support delivery ETA communication:

  estimated_minutes  INTEGER  NULL    — admin-entered ETA (minutes from now)
  eta_communicated   BOOLEAN  FALSE   — flips TRUE once we WhatsApp the customer

Pair with scheduler logic that:
  1. Finds orders where paid=TRUE AND status IN ('confirmado','en_preparacion')
     AND estimated_minutes IS NOT NULL AND eta_communicated = FALSE
  2. Sends a single ETA message via WhatsApp
  3. Marks eta_communicated=TRUE

Partial index: only orders waiting for an ETA send are indexed, so the
scheduler scan stays small as the table grows.

Revision ID: 0064
Revises: 0063
Create Date: 2026-05-04
"""

from alembic import op


revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE orders "
        "ADD COLUMN IF NOT EXISTS estimated_minutes INTEGER DEFAULT NULL"
    )
    op.execute(
        "ALTER TABLE orders "
        "ADD COLUMN IF NOT EXISTS eta_communicated BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_eta_pending "
        "ON orders (status) "
        "WHERE estimated_minutes IS NOT NULL "
        "  AND eta_communicated = FALSE "
        "  AND paid = TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orders_eta_pending")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS eta_communicated")
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS estimated_minutes")
