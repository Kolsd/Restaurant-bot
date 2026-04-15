"""Backfill restaurant_id NULLs + enforce NOT NULL on five tables.

WHY:
  Migration 0026 added restaurant_id as a NULLABLE column to five tables that
  previously scoped rows via bot_number. That was the safe first step — no
  downtime, no data loss. This migration is Step 2: backfill the NULLs,
  verify no orphans remain, then tighten the column to NOT NULL so application
  code can rely on the invariant.

TABLES AFFECTED (all had restaurant_id added by 0026, scoped via bot_number):
  - orders
  - table_orders
  - conversations
  - nps_responses

NOTE: menu_availability also got restaurant_id in 0026 but it has NO bot_number
column (scoping is via dish_name / prior admin action). It was backfilled out-of-band
and is already NOT NULL, so it is OUT OF SCOPE for this migration.

BACKFILL STRATEGY:
  For large tables (orders, conversations may have millions of rows) we use a
  batched UPDATE via CTE with LIMIT 10 000 + FOR UPDATE SKIP LOCKED to avoid
  a multi-minute table lock. The loop runs until zero rows are updated.
  For smaller tables (table_orders, nps_responses, menu_availability) the same
  batched pattern is used for safety — the cost is negligible.

ORPHAN HANDLING:
  Rows whose bot_number does not match any restaurants.whatsapp_number cannot
  be backfilled. After the batched loop we do a COUNT(*) WHERE restaurant_id
  IS NULL. If count > 0 the migration FAILS FAST with the table name, count,
  and a sample of the offending bot_numbers. The operator must resolve orphans
  (delete them or patch restaurants) and re-run.

IRREVERSIBILITY:
  downgrade() drops the NOT NULL constraint and the new indexes but DOES NOT
  restore NULL values. The backfilled restaurant_id data is permanent.

Revision ID: 0027_backfill_tenant_ids
Revises: 0026
Create Date: 2026-04-15
"""

import logging

from alembic import op
from sqlalchemy import text

revision = "0027_backfill_tenant_ids"
down_revision = "0026"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Tables that join on bot_number (menu_availability excluded — no bot_number column,
# already NOT NULL from prior manual backfill).
_BOT_NUMBER_TABLES = [
    "orders",
    "table_orders",
    "conversations",
    "nps_responses",
]

# Batch size for UPDATE loops
_BATCH = 10_000


def _batched_backfill(conn, table: str) -> int:
    """UPDATE restaurant_id in batches of _BATCH rows.

    Returns the total number of rows updated.
    """
    total = 0
    iteration = 0
    while True:
        iteration += 1
        result = conn.execute(text(
            f"""
            WITH batch AS (
                SELECT ctid
                FROM {table}
                WHERE restaurant_id IS NULL
                LIMIT {_BATCH}
                FOR UPDATE SKIP LOCKED
            )
            UPDATE {table} t
            SET restaurant_id = r.id
            FROM restaurants r, batch
            WHERE t.ctid = batch.ctid
              AND t.bot_number = r.whatsapp_number
            RETURNING 1
            """
        ))
        rows = result.rowcount
        total += rows
        if rows > 0:
            logger.info(
                "0027 backfill %s: iteration %d updated %d rows (total so far: %d)",
                table, iteration, rows, total,
            )
        if rows == 0:
            break
    logger.info("0027 backfill %s: DONE — %d rows backfilled", table, total)
    return total


def _check_orphans(conn, table: str) -> None:
    """Raise if any rows still have restaurant_id IS NULL after backfill."""
    result = conn.execute(text(
        f"SELECT COUNT(*) FROM {table} WHERE restaurant_id IS NULL"
    ))
    count = result.scalar()
    if count == 0:
        return

    # Fetch a sample of orphan bot_numbers for the operator
    sample_result = conn.execute(text(
        f"""
        SELECT DISTINCT bot_number
        FROM {table}
        WHERE restaurant_id IS NULL
          AND bot_number IS NOT NULL
        LIMIT 5
        """
    ))
    samples = [row[0] for row in sample_result]

    raise Exception(
        f"0027: table '{table}' has {count} orphan row(s) with restaurant_id IS NULL "
        f"after backfill. These rows have bot_number values that do not match any "
        f"restaurants.whatsapp_number. Sample bot_numbers: {samples!r}. "
        f"Resolve orphans (delete rows or add matching restaurant) and re-run."
    )


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    conn = op.get_bind()

    for table in _BOT_NUMBER_TABLES:
        logger.info("0027: starting backfill for table '%s'", table)
        try:
            _batched_backfill(conn, table)
            _check_orphans(conn, table)
        except Exception as exc:
            # Re-raise so Alembic rolls back the transaction
            raise RuntimeError(
                f"0027 upgrade failed at table '{table}': {exc}"
            ) from exc

        # SET NOT NULL — safe now that orphan check passed
        op.execute(f"ALTER TABLE {table} ALTER COLUMN restaurant_id SET NOT NULL")
        logger.info("0027: SET NOT NULL on %s.restaurant_id", table)

        # Index — partial index on 0026 used WHERE IS NOT NULL; now the column
        # is NOT NULL so a plain index is more efficient.
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_restaurant_id "
            f"ON {table}(restaurant_id)"
        )
        logger.info("0027: index ix_%s_restaurant_id created", table)

    # Drop the partial indexes created in 0026 for the same column (they used
    # WHERE restaurant_id IS NOT NULL which is now redundant and slightly
    # larger than the plain index above).
    _partial_indexes_from_0026 = [
        "ix_nps_responses_restaurant_id",
        "ix_orders_restaurant_id",
        "ix_table_orders_restaurant_id",
    ]
    for idx in _partial_indexes_from_0026:
        op.execute(f"DROP INDEX IF EXISTS {idx}")
        logger.info("0027: dropped partial index %s (replaced by plain index)", idx)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # NOTE: This is IRREVERSIBLE for data. Dropping NOT NULL does not restore
    # the NULL values that were overwritten by the backfill. The backfilled
    # restaurant_id data remains in place; only the constraint is relaxed.

    for table in reversed(_BOT_NUMBER_TABLES):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_restaurant_id")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN restaurant_id DROP NOT NULL"
        )
        logger.info("0027 downgrade: dropped index + NOT NULL on %s.restaurant_id", table)

    # Restore the partial indexes dropped in upgrade (best effort — they may
    # have already existed or may conflict; use IF NOT EXISTS).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_nps_responses_restaurant_id "
        "ON nps_responses(restaurant_id) WHERE restaurant_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_orders_restaurant_id "
        "ON orders(restaurant_id) WHERE restaurant_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_table_orders_restaurant_id "
        "ON table_orders(restaurant_id) WHERE restaurant_id IS NOT NULL"
    )
