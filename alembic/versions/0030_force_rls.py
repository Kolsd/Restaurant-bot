"""FORCE Row-Level Security on all tenant-scoped tables.

WHY:
  Migration 0029 enabled RLS + attached the tenant_isolation policy to every
  tenant-scoped table. Without FORCE, two classes of users bypass RLS:
    1. Postgres SUPERUSERs — cannot be restricted by any SQL mechanism, they
       bypass RLS entirely. The only fix is to stop connecting as superuser.
    2. Table OWNERs — bypass RLS unless FORCE is applied.

  In the current setup, tables are owned by `postgres` (the migration user),
  which happens to also be a superuser. So owner-bypass is moot today. But
  this migration future-proofs the security boundary: if a future ops change
  transfers table ownership to a non-superuser role (e.g. `mesio_owner`), RLS
  still enforces against it. Defense in depth — one less foot-gun.

  FORCE does NOT affect:
    - `mesio_superadmin` (BYPASSRLS role attribute — bypasses regardless).
    - Postgres superusers (bypass is a privilege level above role attrs).

  So: `bypass_tenant_scope` still works, superuser migrations still work, but
  any non-superuser non-BYPASSRLS connection — even if it owns the table — is
  forced through the policy.

TABLES: same 33 listed in migration 0029.

Revision ID: 0030_force_rls
Revises: 0029_enable_row_level_security
Create Date: 2026-04-15
"""

import logging

from alembic import op

revision = "0030_force_rls"
down_revision = "0029_enable_row_level_security"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# Must match the _RLS_TABLES list in migration 0029 exactly.
_RLS_TABLES = [
    "attendance_deductions",
    "billing_log",
    "carts",
    "contract_templates",
    "conversations",
    "customer_profiles",
    "dish_recipes",
    "fiscal_invoices",
    "fiscal_resolution",
    "inventory",
    "loyalty_customers",
    "loyalty_ledger",
    "marketing_messages_log",
    "menu_availability",
    "menu_events",
    "nps_responses",
    "nps_waiting",
    "occupancy_snapshots",
    "orders",
    "overtime_requests",
    "payroll_runs",
    "staff",
    "staff_deduction_items",
    "staff_schedules",
    "staff_shifts",
    "subscription_usage",
    "table_orders",
    "table_sessions",
    "time_slot_discounts",
    "tip_distributions",
    "waiter_alerts",
    "webauthn_challenges",
    "weekly_reports",
]


def upgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        logger.info("0030: FORCE ROW LEVEL SECURITY on %s", table)


def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        logger.info("0030 downgrade: NO FORCE on %s", table)
