"""Add restaurant_id to four tables that were scoped only via bot_number.

WHY:
  Four tables have NO restaurant_id column at all. They rely on bot_number to
  scope rows to a tenant. Before we can enable Row-Level Security (migration
  0029) every table needs a direct, NOT NULL, FK-backed restaurant_id column.

TABLES AFFECTED:
  - carts
  - table_sessions
  - waiter_alerts
  - nps_waiting

BACKFILL STRATEGY:
  Same batched CTE pattern used in 0027: UPDATE in batches of 10 000 rows
  using FOR UPDATE SKIP LOCKED to avoid long table locks. Loop until zero
  rows are left with restaurant_id IS NULL.

ORPHAN HANDLING:
  After the batched loop we COUNT rows still NULL. If count > 0 the migration
  FAILS FAST with table name, count, and a sample of offending bot_numbers.

FOREIGN KEY:
  After NOT NULL is enforced we add a FK to restaurants(id) ON DELETE CASCADE.
  This means deleting a restaurant will cascade-delete all related rows in
  these tables — intentional for full tenant isolation.

IRREVERSIBILITY:
  downgrade() drops the FK, index, and column entirely. The backfilled data
  is lost on rollback. Document this before running downgrade in production.

ASSUMPTION — NULL bot_number rows:
  If a row has bot_number IS NULL it cannot be joined to restaurants. Such
  rows are treated as orphans and will fail the orphan check. The operator
  must decide whether to delete these rows or patch them before running the
  migration. We do NOT silently skip them.

ASSUMPTION — duplicate whatsapp_number in restaurants:
  If two restaurants share the same whatsapp_number the JOIN is ambiguous and
  could assign the wrong restaurant_id. The preflight SQL script
  (0027_0028_preflight.sql) checks for this. The migration itself will pick
  an arbitrary match (Postgres row ordering) in that case — resolve duplicates
  first.

Revision ID: 0028_linked_tables_restaurant_id
Revises: 0027_backfill_tenant_ids
Create Date: 2026-04-15
"""

import logging

from alembic import op
from sqlalchemy import text

revision = "0028_linked_tables_restaurant_id"
down_revision = "0027_backfill_tenant_ids"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# Tables to add restaurant_id to (scoped via bot_number)
# ---------------------------------------------------------------------------

_TABLES = [
    "carts",
    "table_sessions",
    "waiter_alerts",
    "nps_waiting",
]

_BATCH = 10_000


def _batched_backfill(conn, table: str) -> int:
    """UPDATE restaurant_id in batches of _BATCH rows.

    Returns total rows updated.
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
                "0028 backfill %s: iteration %d updated %d rows (total so far: %d)",
                table, iteration, rows, total,
            )
        if rows == 0:
            break
    logger.info("0028 backfill %s: DONE — %d rows backfilled", table, total)
    return total


def _check_orphans(conn, table: str) -> None:
    """Raise if any rows still have restaurant_id IS NULL after backfill."""
    result = conn.execute(text(
        f"SELECT COUNT(*) FROM {table} WHERE restaurant_id IS NULL"
    ))
    count = result.scalar()
    if count == 0:
        return

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

    # Also count rows with NULL bot_number separately for clarity
    null_bot_result = conn.execute(text(
        f"SELECT COUNT(*) FROM {table} WHERE restaurant_id IS NULL AND bot_number IS NULL"
    ))
    null_bot_count = null_bot_result.scalar()

    raise Exception(
        f"0028: table '{table}' has {count} orphan row(s) with restaurant_id IS NULL "
        f"after backfill ({null_bot_count} of those have bot_number IS NULL). "
        f"Sample unmatched bot_numbers: {samples!r}. "
        f"Resolve orphans before re-running."
    )


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    conn = op.get_bind()

    for table in _TABLES:
        logger.info("0028: processing table '%s'", table)
        try:
            # Step 1: Add column (nullable first so it's instant — no table rewrite)
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS restaurant_id INTEGER"
            )
            logger.info("0028: ADD COLUMN restaurant_id on %s", table)

            # Step 2: Batched backfill
            _batched_backfill(conn, table)

            # Step 3: Fail-fast orphan check
            _check_orphans(conn, table)

        except Exception as exc:
            raise RuntimeError(
                f"0028 upgrade failed at table '{table}': {exc}"
            ) from exc

        # Step 4: Enforce NOT NULL
        op.execute(f"ALTER TABLE {table} ALTER COLUMN restaurant_id SET NOT NULL")
        logger.info("0028: SET NOT NULL on %s.restaurant_id", table)

        # Step 5: Add FK to restaurants (ON DELETE CASCADE for full tenant isolation)
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT fk_{table}_restaurant "
            f"FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE"
        )
        logger.info("0028: FK fk_%s_restaurant added", table)

        # Step 6: Index for join and filter performance
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_restaurant_id "
            f"ON {table}(restaurant_id)"
        )
        logger.info("0028: index ix_%s_restaurant_id created", table)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # NOTE: This is IRREVERSIBLE for data. Dropping the column erases all
    # backfilled restaurant_id values. Do NOT run downgrade in production
    # without a data backup.

    for table in reversed(_TABLES):
        op.execute(
            f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_restaurant"
        )
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_restaurant_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS restaurant_id")
        logger.info(
            "0028 downgrade: dropped FK + index + column on %s.restaurant_id", table
        )
