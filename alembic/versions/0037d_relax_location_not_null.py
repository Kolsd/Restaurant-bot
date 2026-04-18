"""Relax NOT NULL on location_id for operational tables during hybrid state.

WHY:
  Wave 2 refactor switched SQL column references from restaurant_id to org_id,
  but the INSERT paths that write `orders`, `table_orders`, `staff_shifts`,
  etc. don't yet explicitly populate `location_id`. Migration 0035 had set
  `location_id NOT NULL` on these tables, assuming triggers + explicit app
  code would fill it. In practice neither reliably does for every INSERT
  path in the hybrid (pre-0037) state.

  Rather than track down every caller in the middle of an outage, we relax
  the constraint. `location_id` is still populated on most paths (via the
  auto-populate triggers when branch_id or restaurant_id is set). Queries
  that filter by location still work against populated rows.

  When 0037 is applied later and app code is fully Wave 2-ready, a follow-up
  migration can re-enforce NOT NULL.

Revision ID: 0037d_relax_location
Revises: 0037c_billing_conf
Create Date: 2026-04-18
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0037d_relax_location"
down_revision = "0037c_billing_conf"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Tables where 0035 set location_id NOT NULL but app INSERT paths may not
# always populate it during the hybrid state. Keep NOT NULL on tables where
# we KNOW the app always provides it (tables that are only inserted via
# admin/config flows with explicit location context).
_RELAX_TABLES = [
    "orders",
    "table_orders",
    "staff",
    "staff_shifts",
    "staff_schedules",
    "staff_deduction_items",
    "attendance_deductions",
    "fiscal_invoices",
    "fiscal_resolution",
    "inventory",
    "menu_availability",
    "occupancy_snapshots",
    "overtime_requests",
    "table_sessions",
    "time_slot_discounts",
    "tip_distributions",
    "waiter_alerts",
    "webauthn_challenges",
]


def upgrade() -> None:
    for table in _RELAX_TABLES:
        # Idempotent: ALTER COLUMN DROP NOT NULL is a no-op if already nullable.
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN location_id DROP NOT NULL"
        ))
        logger.info("0037d: location_id now nullable on %s", table)


def downgrade() -> None:
    # Re-enforcing NOT NULL requires all rows to be populated. If app code
    # hasn't backfilled, this will fail. Safe: operator can backfill first
    # then re-run downgrade.
    for table in _RELAX_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ALTER COLUMN location_id SET NOT NULL"
        ))
        logger.info("0037d downgrade: location_id NOT NULL re-enforced on %s", table)
