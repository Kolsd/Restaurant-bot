"""Money columns: INTEGER → NUMERIC(14,2) for orders and table money fields.

GAP CLOSED:
  orders.subtotal, orders.delivery_fee, orders.total — declared INTEGER in 0001.
  table_orders.total                                  — declared INTEGER in 0001.
  table_sessions.total_spent                          — declared INTEGER in 0001.

  INTEGER silently truncates cents for non-zero-decimal currencies (USD, EUR,
  BRL, MXN). COP tenants are unaffected (zero-decimal), but as Mesio expands
  to new markets this becomes a data-loss risk.

TYPE CHANGE SAFETY:
  ALTER COLUMN ... TYPE NUMERIC(14,2) USING col::NUMERIC(14,2) converts every
  existing integer 28000 → 28000.00 — no data loss. PostgreSQL accepts integer
  literals into NUMERIC columns, so existing COP app code writing integer values
  continues to work without any application change.

  For a large orders table this ALTER takes a brief ACCESS EXCLUSIVE lock to
  rewrite the table. In production, run during low-traffic window or use
  pg_repack if zero-downtime is required. The lock duration is proportional
  to table size (row rewrite). On a typical restaurant deployment (<1M orders)
  this completes in seconds.

DOWNGRADE WARNING:
  Reverting NUMERIC → INTEGER TRUNCATES any fractional cents stored after the
  upgrade. This is noted in code but is acceptable if the downgrade is applied
  before any non-integer amounts are written.

NULL CHECK (informational — logged, not blocking):
  A WARNING is emitted if any NULL values exist in orders.total before the
  conversion, since the column is NOT NULL per the original schema. In practice
  this should never fire.

Revision ID: 0046_money_precision
Revises: 0045_policy_naming
Create Date: 2026-04-20
"""

import logging

from alembic import op

revision = "0046_money_precision"
down_revision = "0045_policy_naming"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# (table, column, nullable)
_MONEY_COLUMNS = [
    ("orders", "subtotal", False),
    ("orders", "delivery_fee", True),   # DEFAULT 0 but no NOT NULL in 0001
    ("orders", "total", False),
    ("table_orders", "total", True),
    ("table_sessions", "total_spent", True),
]


def upgrade() -> None:
    # Pre-check: warn if any NOT NULL column has NULLs (should be 0)
    op.execute("""
        DO $$
        DECLARE null_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO null_count FROM orders WHERE total IS NULL;
            IF null_count > 0 THEN
                RAISE WARNING '0046: orders.total has % NULL rows before type change', null_count;
            END IF;
        END $$;
    """)

    for table, column, _nullable in _MONEY_COLUMNS:
        logger.info("0046 upgrade: converting %s.%s INTEGER → NUMERIC(14,2)", table, column)
        op.execute(f"""
            ALTER TABLE {table}
            ALTER COLUMN {column} TYPE NUMERIC(14,2)
            USING {column}::NUMERIC(14,2)
        """)

    logger.info("0046 upgrade: all money columns converted to NUMERIC(14,2)")


def downgrade() -> None:
    # WARNING: revert truncates any fractional cent values stored after upgrade.
    # Safe to apply only if no non-integer amounts have been written.
    for table, column, _nullable in reversed(_MONEY_COLUMNS):
        logger.info(
            "0046 downgrade: reverting %s.%s NUMERIC(14,2) → INTEGER "
            "(TRUNCATES fractional values!)",
            table, column,
        )
        op.execute(f"""
            ALTER TABLE {table}
            ALTER COLUMN {column} TYPE INTEGER
            USING {column}::INTEGER
        """)

    logger.info("0046 downgrade: all money columns reverted to INTEGER")
