"""Add processed_wompi_events table for Wompi webhook idempotency.

Wompi retries its callback if it does not receive a 2xx within the timeout
window.  Without dedup the same transaction.updated event can confirm an
order, decrement inventory, and notify the customer more than once.

This table records every event_id we have successfully processed.
The handler inserts with ON CONFLICT DO NOTHING and checks the affected
row count: 1 = first time (proceed), 0 = replay (return 200 immediately).

Design notes
------------
* GLOBAL table (no restaurant_id column): the Wompi callback resolves the
  order reference before it knows the restaurant.  Adding RLS here would
  require bypassing it on every write anyway — explicitly kept GLOBAL like
  `sessions` and `webhook_inbox`.  Do NOT add to _RLS_TABLES in 0029/0030.
* event_id is the Wompi transaction UUID (data.transaction.id in the
  webhook payload).  It is stable across retries for the same transaction.
* processed_at index lets a future cleanup job prune old rows cheaply.

Revision ID: 0033_processed_wompi_events
Revises: 0032_deposits_one_pending
Create Date: 2026-04-17
"""
from alembic import op

revision = "0033_processed_wompi_events"
down_revision = "0032_deposits_one_pending"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_wompi_events (
            event_id       TEXT PRIMARY KEY,
            processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            transaction_id TEXT,
            order_id       INTEGER,
            status         TEXT
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_processed_wompi_events_processed_at
            ON processed_wompi_events (processed_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS processed_wompi_events")
