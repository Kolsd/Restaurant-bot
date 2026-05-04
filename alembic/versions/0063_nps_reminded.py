"""
0063 — nps_waiting.reminded_at: track 24h re-trigger of pending NPS surveys.

Background:
  When the bot triggers an NPS survey (table closed / delivery delivered),
  it inserts a row into nps_waiting with created_at=NOW(). If the customer
  doesn't reply with a 1–5 score, the row sits there until 48h cleanup
  removes it. The customer is never asked again — and stores lose ~70% of
  potential NPS data this way.

  This migration enables a 24-hour re-trigger:
    - reminded_at NULL  → first survey was sent, no reminder yet
    - reminded_at set   → reminder already sent (don't send again)

  Pair with scheduler logic that:
    1. Finds rows where created_at is 24-48h old AND reminded_at IS NULL
    2. Sends a single reminder WhatsApp message
    3. Marks reminded_at = NOW()
    4. Cleanup deletes rows older than 48h regardless of state.

  The partial index speeds up the scheduler scan: only rows still pending
  a reminder are indexed. The window predicate intentionally lives in the
  query, not the index, so the index stays small (and the scheduler can
  later widen the window without an index rebuild).

Revision ID: 0063
Revises: 0062
Create Date: 2026-05-03
"""

from alembic import op


revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE nps_waiting "
        "ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ DEFAULT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_nps_waiting_pending_reminder "
        "ON nps_waiting (created_at) WHERE reminded_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_nps_waiting_pending_reminder")
    op.execute("ALTER TABLE nps_waiting DROP COLUMN IF EXISTS reminded_at")
