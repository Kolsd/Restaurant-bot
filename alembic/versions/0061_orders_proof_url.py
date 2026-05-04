"""
0061 — orders.proof_url: persist payment receipt URL for delivery/pickup orders.

Background:
  Manual-proof flow: when Wompi is not configured (or the customer pays via
  Nequi/Daviplata/Bancolombia), the customer pays manually to the restaurant's
  account and sends a screenshot via WhatsApp. Caja validates visually before
  marking the order as paid.

  Until now `orders` had no column to persist this URL. The frontend (caja.js)
  already reads `o.proof_url` for the deliveries grid, but the field was always
  NULL — caja could only see the photo by scrolling through the conversation.

  table_orders has its own attach mechanism via `db_attach_proof` →
  awaiting_proof checks. This adds the equivalent capability for
  delivery/pickup orders living in the `orders` table.

  Stores `/api/media/{id}?bot=...` URLs (or absolute Cloudinary URLs in the
  future). Empty string = no proof attached yet.

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-04
"""

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS proof_url TEXT DEFAULT ''")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS proof_url")
