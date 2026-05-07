"""Add turns_without_progress to conversations table.

Tracks consecutive turns where the customer hasn't advanced toward an order
(no tool call fired). Used by the anti-conversational session-close logic:
  - 4 turns → gentle nudge appended to reply
  - 6 turns → wrap-up message + human_handoff waiter_alert (best-effort)

Revision ID: 0074_conv_turns_without_progress
Revises:     0073_perf_idx_blocklist_check
"""

revision = "0074_conv_turns_without_progress"
down_revision = "0073_perf_idx_blocklist_check"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE conversations
        ADD COLUMN IF NOT EXISTS turns_without_progress INTEGER NOT NULL DEFAULT 0
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE conversations
        DROP COLUMN IF EXISTS turns_without_progress
    """))
