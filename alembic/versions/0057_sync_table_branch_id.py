"""
0057 — Sync restaurant_tables.branch_id with location_id.

Background:
  Pre-Wave-2 data stored `branch_id` as the org_id (the "Matriz invariant"
  trick). Wave-2 introduced `location_id` as the canonical sede id but did
  not backfill `branch_id` to match. Result: a restaurant with org_id=8
  and 3 locations (1, 3, 4) had its tables stored with branch_id=8 even
  when location_id=1 — so any query filtering by sede via branch_id
  returned 0 rows for that org.

  Discovered 2026-04-28 when PM reported "/mesero says No hay mesas
  configuradas". The endpoint /api/pos/tables-status resolved
  location_id=1 correctly, but db_get_tables filtered by branch_id=1 and
  the rows had branch_id=8.

  Code fix (db_get_tables filters by location_id) shipped in the same
  PR/commit. This migration cleans up the data so future queries that
  legitimately use branch_id (legacy callers, sync handlers) also
  see consistent values.

Action:
  UPDATE restaurant_tables SET branch_id = location_id
  WHERE branch_id IS DISTINCT FROM location_id;

Idempotent — safe to re-run. Logged via implicit row count.

Revision ID: 0057
Revises: 0056
Create Date: 2026-04-28
"""

from alembic import op


revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE restaurant_tables
        SET branch_id = location_id
        WHERE branch_id IS DISTINCT FROM location_id
          AND location_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    # No-op: there's no general way to recover the pre-fix branch_id values
    # (they were stale data, not authoritative). If a rollback is ever
    # needed, recover from a pre-migration backup.
    pass
