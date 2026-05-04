"""Add attempts column to weekly_reports for failure retry tracking.

WHY:
  The weekly reports scheduler used to mark a report row as 'failed' on
  WhatsApp send failure but never retried it — once a row existed for
  (org_id, week_start), the ON CONFLICT DO NOTHING in save_report blocked
  any re-attempt. This left clients silently without their Monday digest
  whenever Meta hiccuped.

  We now track attempts per row so the scheduler can detect failed-but-
  retriable reports and try again on the next tick (capped to 3 attempts
  to avoid infinite loops on permanent failures like wrong phone numbers).

NEW COLUMN:
  attempts INTEGER NOT NULL DEFAULT 0   — incremented on each send attempt

Idempotent via IF NOT EXISTS — safe to re-run on partially-migrated databases.

Revision ID: 0067_weekly_reports_retry
Revises: 0066_review_reply_deliv
Create Date: 2026-05-04
"""

from alembic import op


revision = "0067_weekly_reports_retry"
down_revision = "0066_review_reply_deliv"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE weekly_reports "
        "ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE weekly_reports DROP COLUMN IF EXISTS attempts")
