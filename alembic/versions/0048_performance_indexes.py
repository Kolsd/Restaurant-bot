"""Performance indexes: 6 composite indexes for hot query paths.

INDEX RATIONALE:
  1. ix_orders_org_created           — dashboard "orders today/this week" queries
                                       filter by org_id + date range.
  2. ix_table_orders_org_created     — KDS + kitchen views scan active table orders
                                       for an org ordered by recency.
  3. ix_staff_shifts_staff_clock     — timecard queries: "all shifts for staff X
                                       in date range" hits this composite.
  4. ix_conversations_bot_phone      — inbox_worker._handle_meta_whatsapp looks up
                                       conversation by (bot_number, phone). Existing
                                       idx_convs_updated covers (bot_number, updated_at)
                                       but not the phone lookup.
  5. ix_webhook_inbox_claim          — the claim-then-ack fetch query:
                                       WHERE processed_at IS NULL
                                       ORDER BY next_attempt_at, id
                                       Partial index eliminates processed rows.
  6. ix_nps_responses_org_created    — NPS dashboard analytics scan by org + date.

Revision ID: 0048_performance_indexes
Revises: 0047_swap_status_check
Create Date: 2026-04-20
"""

import logging

from alembic import op

revision = "0048_performance_indexes"
down_revision = "0047_swap_status_check"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# (index_name, create_sql, drop_sql)
_INDEXES = [
    (
        "ix_orders_org_created",
        "CREATE INDEX IF NOT EXISTS ix_orders_org_created "
        "ON orders (org_id, created_at DESC)",
        "DROP INDEX IF EXISTS ix_orders_org_created",
    ),
    (
        "ix_table_orders_org_created",
        "CREATE INDEX IF NOT EXISTS ix_table_orders_org_created "
        "ON table_orders (org_id, created_at DESC)",
        "DROP INDEX IF EXISTS ix_table_orders_org_created",
    ),
    (
        "ix_staff_shifts_staff_clock",
        "CREATE INDEX IF NOT EXISTS ix_staff_shifts_staff_clock "
        "ON staff_shifts (staff_id, clock_in DESC, clock_out)",
        "DROP INDEX IF EXISTS ix_staff_shifts_staff_clock",
    ),
    (
        "ix_conversations_bot_phone",
        "CREATE INDEX IF NOT EXISTS ix_conversations_bot_phone "
        "ON conversations (bot_number, phone)",
        "DROP INDEX IF EXISTS ix_conversations_bot_phone",
    ),
    (
        "ix_webhook_inbox_claim",
        "CREATE INDEX IF NOT EXISTS ix_webhook_inbox_claim "
        "ON webhook_inbox (next_attempt_at, id) WHERE processed_at IS NULL",
        "DROP INDEX IF EXISTS ix_webhook_inbox_claim",
    ),
    (
        "ix_nps_responses_org_created",
        "CREATE INDEX IF NOT EXISTS ix_nps_responses_org_created "
        "ON nps_responses (org_id, created_at DESC)",
        "DROP INDEX IF EXISTS ix_nps_responses_org_created",
    ),
]


def upgrade() -> None:
    for name, create_sql, _drop_sql in _INDEXES:
        logger.info("0048 upgrade: creating index %s", name)
        op.execute(create_sql)

    logger.info("0048 upgrade: all 6 performance indexes created")


def downgrade() -> None:
    for name, _create_sql, drop_sql in reversed(_INDEXES):
        logger.info("0048 downgrade: dropping index %s", name)
        op.execute(drop_sql)

    logger.info("0048 downgrade: all 6 performance indexes dropped")
