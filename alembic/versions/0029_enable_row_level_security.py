"""Enable Row-Level Security on all tenant-scoped tables.

WHY:
  This migration is the security boundary that makes the Python-layer
  tenant_scope() plumbing actually enforce isolation. Without RLS policies,
  the `SELECT set_config('app.restaurant_id', $1, true)` executed by
  tenant_connection() is a no-op — nothing reads the GUC. Once this migration
  runs, every SELECT/UPDATE/DELETE against a tenant-scoped table is auto-filtered
  by Postgres using `current_setting('app.restaurant_id')`.

DEFENSE IN DEPTH:
  Python layer (tenant_context.py) raises TenantNotSetError BEFORE a query runs
  if no scope is active — fast, clear error for developers.
  DB layer (RLS policy) fails closed if the GUC is missing or wrong — absolute
  guarantee against cross-tenant data leaks, even if the Python layer has a bug.

POLICY SEMANTICS:
  USING  (SELECT) — a row is visible only if its restaurant_id matches the GUC.
  WITH CHECK (INSERT/UPDATE) — a row can be written only with the correct
                                restaurant_id. Prevents tenant-ID spoofing.
  NULLIF(current_setting('app.restaurant_id', true), '')::int
    - missing_ok=true  → returns NULL instead of erroring if unset
    - NULLIF '' → NULL → comparison returns NULL → row hidden (fail-closed)

BYPASSRLS ROLE:
  mesio_superadmin is created with the BYPASSRLS attribute so that
  bypass_tenant_scope() can do cross-tenant admin operations (internal routes,
  scheduler leader tick, analytics). current_user is GRANTed this role so
  SET LOCAL ROLE works.

⚠️  SUPERUSER CAVEAT — READ CAREFULLY ⚠️
  Postgres superusers ALWAYS bypass RLS, regardless of policies or FORCE. If
  the app connects as a superuser (as in this dev DB where DATABASE_URL points
  to the `postgres` role), this migration is effectively a NO-OP for the app.
  For production enforcement you MUST:
    1. Create a non-superuser role (e.g. `mesio_app`) with the minimum needed
       privileges (SELECT/INSERT/UPDATE/DELETE on app tables).
    2. GRANT mesio_superadmin TO mesio_app (for bypass_tenant_scope).
    3. Update DATABASE_URL to connect as `mesio_app` instead of `postgres`.
  This migration is still safe to apply in superuser envs — it just doesn't
  enforce anything there. The moment you migrate to a non-superuser app role,
  enforcement becomes active.

FORCE NOT USED:
  ALTER TABLE ... FORCE ROW LEVEL SECURITY makes RLS apply even to the table
  OWNER. We do NOT apply FORCE in this migration. A future migration can add
  FORCE after validating the non-superuser setup works in staging.

TABLES (33):
  Derived by querying information_schema for every public table with a
  NOT NULL restaurant_id column as of the DB state after migration 0028.
  Some tenant-adjacent tables (reservations with nullable restaurant_id,
  table_checks via FK only, webauthn_credentials via staff FK, restaurant_tables
  via branch_id) are NOT included — they are scoped indirectly via their parent
  tables' RLS policies plus FK CASCADE. A follow-up migration can tighten them.

Revision ID: 0029_enable_row_level_security
Revises: 0028_linked_tables_restaurant_id
Create Date: 2026-04-15
"""

import logging

from alembic import op

revision = "0029_enable_row_level_security"
down_revision = "0028_linked_tables_restaurant_id"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# Tables with restaurant_id NOT NULL — enumerated from information_schema on
# 2026-04-15 against the DB at head=0028. If new tables with restaurant_id are
# added later, they need their own ENABLE RLS + POLICY in a follow-up migration.
# ---------------------------------------------------------------------------

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

_POLICY_NAME = "tenant_isolation"
_POLICY_EXPR = (
    "restaurant_id = NULLIF(current_setting('app.restaurant_id', true), '')::int"
)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    # 1. Create mesio_superadmin role (idempotent) + grant to current user.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mesio_superadmin') THEN
                CREATE ROLE mesio_superadmin BYPASSRLS NOINHERIT;
            END IF;
        END $$;
        """
    )
    op.execute("GRANT mesio_superadmin TO current_user")
    logger.info("0029: mesio_superadmin role ready + granted to current_user")

    # 2. Enable RLS + attach the tenant_isolation policy on every table.
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # Drop policy if it somehow already exists (idempotent re-apply).
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
        op.execute(
            f"""
            CREATE POLICY {_POLICY_NAME} ON {table}
                USING ({_POLICY_EXPR})
                WITH CHECK ({_POLICY_EXPR})
            """
        )
        logger.info("0029: RLS enabled + policy attached on %s", table)


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {_POLICY_NAME} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        logger.info("0029 downgrade: RLS disabled on %s", table)

    # REVOKE then DROP ROLE — only safe if no other dependency; log before.
    op.execute("REVOKE mesio_superadmin FROM current_user")
    op.execute("DROP ROLE IF EXISTS mesio_superadmin")
    logger.info("0029 downgrade: mesio_superadmin role dropped")
