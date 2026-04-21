"""Performance indexes: 7 composite indexes for hot query paths.

WHY CONCURRENT:
  All indexes are created with CREATE INDEX CONCURRENTLY so they do not take
  an ACCESS EXCLUSIVE lock on the table. This means the migration is safe to
  run against a live production database without downtime.

  CONCURRENTLY requires running OUTSIDE a transaction block. This migration
  uses disable_ddl_transaction = True (Alembic's mechanism) so Alembic does
  not wrap the statements in BEGIN/COMMIT. The alembic_version update is still
  transactional because Alembic handles it separately.

  IF NOT EXISTS makes every CREATE idempotent — safe to re-run.

INDEX RATIONALE:
  1. ix_orders_org_created           — dashboard "orders today/this week" queries
                                       filter by org_id + date range.
  2. ix_table_orders_org_created     — KDS + kitchen views scan active table orders
                                       for an org ordered by recency.
  3. ix_table_orders_session         — partial index: table_sessions JOIN only needs
                                       rows where session is set (non-null).
  4. ix_staff_shifts_staff_clock     — timecard queries: "all shifts for staff X
                                       in date range" hits this composite.
  5. ix_conversations_bot_phone      — inbox_worker._handle_meta_whatsapp looks up
                                       conversation by (bot_number, phone). Existing
                                       idx_convs_updated covers (bot_number, updated_at)
                                       but not the phone lookup.
  6. ix_webhook_inbox_claim          — the claim-then-ack fetch query:
                                       WHERE processed_at IS NULL
                                       ORDER BY next_attempt_at, id
                                       Partial index eliminates processed rows.
  7. ix_nps_responses_org_created    — NPS dashboard analytics scan by org + date.

DOWNGRADE:
  All indexes are dropped with DROP INDEX CONCURRENTLY IF EXISTS — also
  non-blocking. Downgrade also runs outside a transaction block.

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

# REQUIRED: CONCURRENTLY cannot run inside a transaction block.
disable_ddl_transaction = True

logger = logging.getLogger("alembic.runtime.migration")

# (index_name, create_sql, drop_sql)
_INDEXES = [
    (
        "ix_orders_org_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_orders_org_created "
        "ON orders (org_id, created_at DESC)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_orders_org_created",
    ),
    (
        "ix_table_orders_org_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_table_orders_org_created "
        "ON table_orders (org_id, created_at DESC)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_table_orders_org_created",
    ),
    (
        "ix_table_orders_session",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_table_orders_session "
        "ON table_orders (table_session_id) WHERE table_session_id IS NOT NULL",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_table_orders_session",
    ),
    (
        "ix_staff_shifts_staff_clock",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_staff_shifts_staff_clock "
        "ON staff_shifts (staff_id, clock_in DESC, clock_out)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_staff_shifts_staff_clock",
    ),
    (
        "ix_conversations_bot_phone",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_conversations_bot_phone "
        "ON conversations (bot_number, phone)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_conversations_bot_phone",
    ),
    (
        "ix_webhook_inbox_claim",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_webhook_inbox_claim "
        "ON webhook_inbox (next_attempt_at, id) WHERE processed_at IS NULL",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_webhook_inbox_claim",
    ),
    (
        "ix_nps_responses_org_created",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nps_responses_org_created "
        "ON nps_responses (org_id, created_at DESC)",
        "DROP INDEX CONCURRENTLY IF EXISTS ix_nps_responses_org_created",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    bind.execution_options(isolation_level="AUTOCOMMIT")

    for name, create_sql, _drop_sql in _INDEXES:
        logger.info("0048 upgrade: creating index %s", name)
        op.execute(create_sql)

    logger.info("0048 upgrade: all 7 performance indexes created")


def downgrade() -> None:
    bind = op.get_bind()
    bind.execution_options(isolation_level="AUTOCOMMIT")

    for name, _create_sql, drop_sql in reversed(_INDEXES):
        logger.info("0048 downgrade: dropping index %s", name)
        op.execute(drop_sql)

    logger.info("0048 downgrade: all 7 performance indexes dropped")
