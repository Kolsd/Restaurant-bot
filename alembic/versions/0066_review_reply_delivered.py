"""Add owner_reply_delivered_at column to nps_responses.

WHY:
  The /api/reviews/{id}/reply endpoint historically only persisted the owner
  reply text. To close the loop with the customer we now best-effort send the
  reply over WhatsApp when phone+bot_number+access_token are available. This
  column records the moment we successfully sent the WA so retries (manual or
  scheduled) can detect already-delivered rows and skip them.

NEW COLUMN:
  owner_reply_delivered_at TIMESTAMPTZ NULL   — set by db_mark_review_reply_delivered

Idempotent via IF NOT EXISTS — safe to re-run on partially-migrated databases.

Revision ID: 0066_review_reply_delivered
Revises: 0065_sub_usage_orders
Create Date: 2026-05-04
"""

from alembic import op


revision = "0066_review_reply_deliv"
down_revision = "0065_sub_usage_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE nps_responses "
        "ADD COLUMN IF NOT EXISTS owner_reply_delivered_at TIMESTAMPTZ DEFAULT NULL"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE nps_responses DROP COLUMN IF EXISTS owner_reply_delivered_at"
    )
