"""Add partial UNIQUE INDEX on reservation_deposits to prevent duplicate pending rows.

Ensures that at most one deposit can be in 'pending' status per reservation_id.
This closes the race condition in db_confirm_deposit where non-deterministic
UPDATE behaviour could affect the wrong row when multiple pending deposits exist
for the same reservation.

Uses plain CREATE UNIQUE INDEX IF NOT EXISTS (no CONCURRENTLY) because
CONCURRENTLY is incompatible with Alembic's transactional DDL:
set_isolation_level rolls back the pending alembic_version update and leaves
the migration state inconsistent. The reservation_deposits table is small
enough that a brief exclusive lock is acceptable.

Revision ID: 0032_deposits_one_pending
Revises: 0031_drop_orphan_sales_tables
Create Date: 2026-04-16
"""
from alembic import op

revision = "0032_deposits_one_pending"
down_revision = "0031_drop_orphan_sales_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS makes this idempotent: if the index was already created by
    # a previous partial run (e.g. with CONCURRENTLY), this is a no-op.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_reservation_deposits_one_pending
        ON reservation_deposits (reservation_id)
        WHERE status = 'pending'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_reservation_deposits_one_pending")
