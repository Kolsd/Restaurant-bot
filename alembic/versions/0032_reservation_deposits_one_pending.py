"""Add partial UNIQUE INDEX on reservation_deposits to prevent duplicate pending rows.

Ensures that at most one deposit can be in 'pending' status per reservation_id.
This closes the race condition in db_confirm_deposit where non-deterministic
UPDATE behaviour could affect the wrong row when multiple pending deposits exist
for the same reservation.

Uses CREATE UNIQUE INDEX CONCURRENTLY so the operation does not lock the table
in production.  CONCURRENTLY requires autocommit mode (no surrounding transaction).
IF NOT EXISTS makes it safe to rerun after partial failure.

Revision ID: 0032_reservation_deposits_one_pending
Revises: 0031_drop_orphan_sales_tables
Create Date: 2026-04-16
"""
from alembic import op

revision      = "0032_reservation_deposits_one_pending"
down_revision = "0031_drop_orphan_sales_tables"
branch_labels = None
depends_on    = None


def _run_concurrently(sql: str) -> None:
    """Execute DDL outside a transaction (required for CONCURRENTLY)."""
    conn = op.get_bind()
    raw_conn = conn.connection
    old_isolation = raw_conn.isolation_level
    raw_conn.set_isolation_level(0)  # AUTOCOMMIT
    try:
        raw_conn.cursor().execute(sql)
    finally:
        raw_conn.set_isolation_level(old_isolation)


def upgrade() -> None:
    _run_concurrently(
        """
        CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
            ux_reservation_deposits_one_pending
        ON reservation_deposits (reservation_id)
        WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    _run_concurrently(
        "DROP INDEX CONCURRENTLY IF EXISTS ux_reservation_deposits_one_pending"
    )
