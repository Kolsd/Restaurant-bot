"""Add org_isolation RLS policy and auto-populate triggers — Dual RLS (Bloque S2).

WHY:
  Migration 0035 added org_id and location_id columns to all 33 RLS tables and
  backfilled them from the restaurant→location mapping. This migration wires up
  the *new* RLS policy so that application code can authenticate via org_id
  (SET app.org_id) while legacy code continues to work via restaurant_id
  (SET app.restaurant_id). The two policies run PERMISSIVE (Postgres OR semantics),
  so a row is visible if EITHER policy passes.

POLICY DESIGN:
  OLD policy (kept, not dropped — that's Bloque S6/migration 0037):
    tenant_isolation: restaurant_id = NULLIF(current_setting('app.restaurant_id',...))::int

  NEW policy (added here, on _RLS_TABLES only):
    org_isolation: org_id = NULLIF(current_setting('app.org_id',...))::bigint

  With two PERMISSIVE policies active, a SELECT passes if:
    (restaurant_id matches app.restaurant_id) OR (org_id matches app.org_id)

  This allows Wave 1 app code (which sets BOTH GUCs) and legacy code (which
  sets only app.restaurant_id) to coexist during the dual-operation period.

  EXTRA TABLES (triggers only, no new RLS policy — not in 0029 _RLS_TABLES):
    restaurant_tables  — has org_id + location_id (0035), uses branch_id as primary key
    reservations       — has org_id + location_id (0035), has branch_id
    reservation_deposits — has org_id + location_id (0035), no restaurant_id column;
                           trigger not needed (columns already backfilled by 0035 and
                           new rows are inserted by application code that provides org_id).

AUTO-POPULATE TRIGGERS:
  Old application code continues to INSERT rows with only restaurant_id set —
  org_id and location_id will be NULL. Without help, the org_isolation WITH CHECK
  would reject those inserts (NULL != anything). We solve this with BEFORE INSERT
  triggers on every RLS table and the relevant extra tables:

  - _auto_populate_org_only()
    Used for: Pure org-level tables (no location_id column at all — billing_log,
    contract_templates, customer_profiles, etc.). Fills org_id only. Using any
    other function would fail at CREATE TRIGGER because NEW.location_id would
    reference a non-existent column.

  - _auto_populate_org_location()
    Used for: Location-level tables WITHOUT branch_id (attendance_deductions,
    fiscal_invoices, staff, inventory, etc.). Fills BOTH org_id and location_id
    from restaurant_id → mapping. These tables have NOT NULL location_id so
    old code (insert with restaurant_id only) would fail without this trigger.

  - _auto_populate_org_location_branch()
    Used for: Location-level tables that HAVE a branch_id column (also includes
    carts/conversations with nullable location_id). Fills org_id, then fills
    location_id from COALESCE(branch_id, restaurant_id) → mapping.
    Leaves location_id NULL if resolver is NULL (correct for nullable-loc tables).

  - _auto_populate_org_location_branch_only()
    Used for: restaurant_tables (has branch_id but NO restaurant_id column).
    Fills both org_id and location_id from branch_id alone.

  All functions use SECURITY DEFINER so they can read the mapping table
  regardless of the caller's role. EXECUTE is granted to mesio_app.

  Trigger name pattern: trg_auto_org_location_<table>

WITH CHECK ordering:
  Postgres evaluates BEFORE ROW triggers BEFORE RLS WITH CHECK. So the trigger
  fills org_id first, then the WITH CHECK sees the filled value. This avoids
  the "INSERT rejected because org_id is NULL" problem for legacy paths.

TABLE CLASSIFICATION:
  ┌──────────────────────────┬────────────┬────────────┬────────────────────────────────────────┐
  │ Table                    │ location_id│ branch_id  │ Trigger function                       │
  ├──────────────────────────┼────────────┼────────────┼────────────────────────────────────────┤
  │ billing_log              │ No         │ No         │ _auto_populate_org_only                │
  │ contract_templates       │ No         │ No         │ _auto_populate_org_only                │
  │ customer_profiles        │ No         │ No         │ _auto_populate_org_only                │
  │ dish_recipes             │ No         │ No         │ _auto_populate_org_only                │
  │ loyalty_customers        │ No         │ No         │ _auto_populate_org_only                │
  │ marketing_messages_log   │ No         │ No         │ _auto_populate_org_only                │
  │ subscription_usage       │ No         │ No         │ _auto_populate_org_only                │
  │ attendance_deductions    │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ fiscal_invoices          │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ fiscal_resolution        │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ inventory                │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ menu_availability        │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ overtime_requests        │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ staff                    │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ staff_deduction_items    │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ staff_schedules          │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ staff_shifts             │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ tip_distributions        │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ webauthn_challenges      │ NOT NULL   │ No         │ _auto_populate_org_location            │
  │ loyalty_ledger           │ nullable   │ No         │ _auto_populate_org_location            │
  │ menu_events              │ nullable   │ No         │ _auto_populate_org_location            │
  │ payroll_runs             │ nullable   │ No         │ _auto_populate_org_location            │
  │ weekly_reports           │ nullable   │ No         │ _auto_populate_org_location            │
  │ occupancy_snapshots      │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ table_sessions           │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ time_slot_discounts      │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ waiter_alerts            │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ orders                   │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ table_orders             │ NOT NULL   │ Yes        │ _auto_populate_org_location_branch     │
  │ carts                    │ nullable   │ Yes        │ _auto_populate_org_location_branch     │
  │ conversations            │ nullable   │ Yes        │ _auto_populate_org_location_branch     │
  │ nps_responses            │ nullable   │ Yes        │ _auto_populate_org_location_branch     │
  │ nps_waiting              │ nullable   │ Yes        │ _auto_populate_org_location_branch     │
  ├──────────────────────────┼────────────┼────────────┼────────────────────────────────────────┤
  │ EXTRA (trigger only, no new RLS policy):                                                   │
  │ restaurant_tables        │ NOT NULL   │ Yes (only) │ _auto_populate_org_location_branch_only│
  │ reservations             │ nullable   │ Yes        │ _auto_populate_org_location_branch     │
  │ reservation_deposits     │ NOT NULL   │ No (FK→res)│ (no trigger — no restaurant_id column) │
  └──────────────────────────┴────────────┴────────────┴────────────────────────────────────────┘

IDEMPOTENCY:
  - DROP POLICY IF EXISTS before CREATE POLICY
  - DROP TRIGGER IF EXISTS before CREATE TRIGGER
  - CREATE OR REPLACE FUNCTION for trigger functions

DO NOT DROP tenant_isolation. That is migration 0037 (Bloque S6).

Revision ID: 0036_dual_rls_policies
Revises: 0035_add_org_location_columns
Create Date: 2026-04-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0036_dual_rls_policies"
down_revision = "0035_add_org_location_columns"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# The canonical RLS table list (identical to 0029 — NOT imported to avoid
# cross-migration import coupling which breaks Alembic's revision graph).
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

# New policy name / expression.
_NEW_POLICY = "org_isolation"
_NEW_POLICY_EXPR = (
    "org_id = NULLIF(current_setting('app.org_id', true), '')::bigint"
)

# ---------------------------------------------------------------------------
# Table classification for triggers
# (table, has_branch_id) from the 0035 migration classification
# ---------------------------------------------------------------------------

# Tables that have a branch_id column. For these we create the "with branch"
# trigger variant that can resolve location_id from branch_id.
_TABLES_WITH_BRANCH_ID = {
    # Only tables with a real branch_id column in the schema.
    # (Must match _ORG_AND_LOCATION_TABLES classification in migration 0035.)
    "occupancy_snapshots",   # 0017
    "time_slot_discounts",   # 0015
    "table_orders",          # 0013
    "conversations",         # 0026
    "nps_responses",         # 0026
    # NOT included (no branch_id column):
    #   orders, table_sessions, waiter_alerts, carts, nps_waiting
}

# Tables where location_id column is nullable by design (per §2.3 / 0035).
# For these the trigger leaves location_id NULL if it can't resolve — that's
# intentional; these tables start without a Location and bind one lazily.
_NULLABLE_LOCATION_TABLES = {
    "carts",
    "conversations",
    "loyalty_ledger",
    "menu_events",
    "nps_responses",
    "nps_waiting",
    "payroll_runs",
    "weekly_reports",
}

# Tables that don't have a location_id column at all (Org-level only).
# These only get org_id populated by the trigger; no location_id touch.
_ORG_ONLY_TABLES = {
    "billing_log",
    "contract_templates",
    "customer_profiles",
    "dish_recipes",
    "loyalty_customers",
    "marketing_messages_log",
    "subscription_usage",
}

# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    # ── Step 1: Create trigger functions ────────────────────────────────────────

    # Function 1: tables without branch_id.
    # Covers two sub-cases:
    #   a) Org-level tables with no location_id column at all (billing_log, etc.)
    #      → only org_id is touched; location_id reference would fail at CREATE.
    #      BUT: since Postgres trigger functions are polymorphic (RETURNS TRIGGER),
    #      referencing NEW.location_id would fail only if the column doesn't exist.
    #      We use TG_TABLE_NAME and a dynamic approach — but that's complex.
    #      Simpler: use a separate minimal function for pure-org tables.
    #   b) Location-level tables with location_id NOT NULL but no branch_id
    #      (attendance_deductions, fiscal_invoices, etc.)
    #      → fill BOTH org_id and location_id from restaurant_id mapping.
    #
    # Resolution: because Postgres can't conditionally reference a column that
    # might not exist in a static trigger function, we handle these two groups
    # with two separate functions:
    #   - _auto_populate_org_only()     → for pure org-level tables (no location_id)
    #   - _auto_populate_org_location() → for location-level tables without branch_id
    #
    # NOTE: The classification sets in the Python code below assign the correct
    # function to each table.

    logger.info("0036: creating _auto_populate_org_only function")
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
            -- Fill org_id only (table has no location_id column).
            IF NEW.org_id IS NULL AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id
                INTO NEW.org_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;
            RETURN NEW;
        END;
        $$
    """))

    logger.info("0036: creating _auto_populate_org_location function")
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
            -- Fill org_id and location_id from restaurant_id (no branch_id on this table).
            IF (NEW.org_id IS NULL OR NEW.location_id IS NULL)
               AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id, m.location_id
                INTO NEW.org_id, NEW.location_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;
            RETURN NEW;
        END;
        $$
    """))

    # Function 2: Location-level tables that have a branch_id column.
    # Fills both org_id and location_id, preferring branch_id over restaurant_id
    # for location resolution (same COALESCE logic as 0035 backfill).
    logger.info("0036: creating _auto_populate_org_location_branch function")
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        DECLARE
            v_resolver BIGINT;
        BEGIN
            -- Fill org_id from restaurant_id mapping.
            IF NEW.org_id IS NULL AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id
                INTO NEW.org_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;

            -- Fill location_id from branch_id (preferred) or restaurant_id (fallback).
            -- NULL result is intentional for nullable-location tables (e.g. carts,
            -- conversations) — those bind a Location lazily at checkout/GPS time.
            IF NEW.location_id IS NULL THEN
                -- Safely read branch_id if present (dynamic column access via hstore
                -- is unavailable, so we use a CASE across known column presence).
                -- Since this function is used only for tables with branch_id, we
                -- can reference it directly. Postgres will error at CREATE time if
                -- the column does not exist, which is the desired fail-fast behavior.
                v_resolver := COALESCE(NEW.branch_id, NEW.restaurant_id);
                IF v_resolver IS NOT NULL THEN
                    SELECT m.location_id
                    INTO NEW.location_id
                    FROM _migration_restaurant_to_location m
                    WHERE m.old_restaurant_id = v_resolver;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$
    """))

    # Function 3: restaurant_tables — has branch_id but NO restaurant_id column.
    # The branch_id IS the tenant key (it maps to a restaurant/location directly).
    # Both org_id and location_id are resolved exclusively from branch_id.
    logger.info("0036: creating _auto_populate_org_location_branch_only function")
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
            -- Fill org_id and location_id from branch_id alone (no restaurant_id column).
            -- This function is used exclusively for restaurant_tables where branch_id
            -- is the only tenant key.
            IF (NEW.org_id IS NULL OR NEW.location_id IS NULL)
               AND NEW.branch_id IS NOT NULL THEN
                SELECT m.org_id, m.location_id
                INTO NEW.org_id, NEW.location_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.branch_id;
            END IF;
            RETURN NEW;
        END;
        $$
    """))

    # Grant EXECUTE to mesio_app (non-superuser app runtime role).
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION _auto_populate_org_only() TO mesio_app"
    ))
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION _auto_populate_org_location() TO mesio_app"
    ))
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION _auto_populate_org_location_branch() TO mesio_app"
    ))
    op.execute(sa.text(
        "GRANT EXECUTE ON FUNCTION _auto_populate_org_location_branch_only() TO mesio_app"
    ))
    logger.info("0036: trigger functions created + EXECUTE granted to mesio_app")

    # ── Step 2: Add org_isolation policy + triggers on each RLS table ───────────
    for table in _RLS_TABLES:
        # Add new RLS policy (org_id-based).
        op.execute(sa.text(
            f"DROP POLICY IF EXISTS {_NEW_POLICY} ON {table}"
        ))
        op.execute(sa.text(
            f"""
            CREATE POLICY {_NEW_POLICY} ON {table}
                USING ({_NEW_POLICY_EXPR})
                WITH CHECK ({_NEW_POLICY_EXPR})
            """
        ))
        logger.info("0036: org_isolation policy created on %s", table)

        # Choose the correct trigger function.
        if table in _ORG_ONLY_TABLES:
            # No location_id column on this table at all. Reference NEW.location_id
            # would fail at CREATE TRIGGER time — use the org-only variant.
            fn = "_auto_populate_org_only"
        elif table in _TABLES_WITH_BRANCH_ID:
            # Has branch_id column: resolve both org_id and location_id, preferring
            # branch_id over restaurant_id for the location resolver.
            fn = "_auto_populate_org_location_branch"
        else:
            # Has location_id but no branch_id: fill both org_id and location_id
            # from restaurant_id → mapping. These are Location-level tables like
            # attendance_deductions, fiscal_invoices, staff, inventory, etc. where
            # old code inserts with restaurant_id only and location_id would
            # violate NOT NULL without trigger help.
            fn = "_auto_populate_org_location"

        trigger_name = f"trg_auto_org_location_{table}"
        op.execute(sa.text(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}"
        ))
        op.execute(sa.text(
            f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION {fn}()
            """
        ))
        logger.info("0036: trigger %s created on %s (fn=%s)", trigger_name, table, fn)

    logger.info("0036 upgrade: all %d RLS tables patched", len(_RLS_TABLES))

    # ── Step 3: Triggers on extra tables (no RLS policy added) ──────────────────
    # These tables have org_id + location_id from migration 0035 but are NOT in
    # the 0029 _RLS_TABLES, so they have no tenant_isolation policy and we don't
    # add org_isolation either. However, they still need triggers to auto-populate
    # org_id/location_id for rows inserted via old code paths.

    # restaurant_tables: branch_id is the only tenant key (no restaurant_id column)
    _extra_branch_only = "restaurant_tables"
    op.execute(sa.text(
        f"DROP TRIGGER IF EXISTS trg_auto_org_location_{_extra_branch_only} "
        f"ON {_extra_branch_only}"
    ))
    op.execute(sa.text(
        f"""
        CREATE TRIGGER trg_auto_org_location_{_extra_branch_only}
        BEFORE INSERT ON {_extra_branch_only}
        FOR EACH ROW
        EXECUTE FUNCTION _auto_populate_org_location_branch_only()
        """
    ))
    logger.info("0036: trigger trg_auto_org_location_%s created", _extra_branch_only)

    # reservations: has restaurant_id (nullable) + branch_id + org_id + location_id
    _extra_branch = "reservations"
    op.execute(sa.text(
        f"DROP TRIGGER IF EXISTS trg_auto_org_location_{_extra_branch} "
        f"ON {_extra_branch}"
    ))
    op.execute(sa.text(
        f"""
        CREATE TRIGGER trg_auto_org_location_{_extra_branch}
        BEFORE INSERT ON {_extra_branch}
        FOR EACH ROW
        EXECUTE FUNCTION _auto_populate_org_location_branch()
        """
    ))
    logger.info("0036: trigger trg_auto_org_location_%s created", _extra_branch)

    # reservation_deposits: no restaurant_id column; org_id/location_id are
    # always provided by the application code that creates deposits (via the
    # reservations FK chain). No trigger needed.
    logger.info(
        "0036: reservation_deposits skipped (no restaurant_id column; "
        "app code always supplies org_id/location_id)"
    )

    # ── Step 4: Validation block ──────────────────────────────────────────────────
    logger.info("0036: running post-migration validation")
    all_policy_tables = list(_RLS_TABLES)  # policies only on _RLS_TABLES
    all_trigger_tables = list(_RLS_TABLES) + ["restaurant_tables", "reservations"]
    # Build VALUES list for the validation queries.
    policy_values = ", ".join(f"('{t}')" for t in all_policy_tables)
    trigger_values = ", ".join(f"('{t}')" for t in all_trigger_tables)

    op.execute(sa.text(f"""
        DO $$
        DECLARE
            missing_policies INT;
            missing_triggers INT;
        BEGIN
            -- Check: every RLS table has the org_isolation policy.
            SELECT COUNT(*) INTO missing_policies
            FROM (VALUES {policy_values}) AS t(name)
            WHERE NOT EXISTS (
                SELECT 1 FROM pg_policies p
                WHERE p.tablename = t.name
                  AND p.policyname = 'org_isolation'
                  AND p.schemaname = 'public'
            );
            IF missing_policies > 0 THEN
                RAISE EXCEPTION
                    'Migration 0036: % table(s) missing org_isolation policy',
                    missing_policies;
            END IF;

            -- Check: every target table has the auto-populate trigger.
            SELECT COUNT(*) INTO missing_triggers
            FROM (VALUES {trigger_values}) AS t(name)
            WHERE NOT EXISTS (
                SELECT 1 FROM pg_trigger trg
                JOIN pg_class cls ON cls.oid = trg.tgrelid
                WHERE cls.relname = t.name
                  AND trg.tgname = 'trg_auto_org_location_' || t.name
                  AND NOT trg.tgisinternal
            );
            IF missing_triggers > 0 THEN
                RAISE EXCEPTION
                    'Migration 0036: % table(s) missing trg_auto_org_location_<table> trigger',
                    missing_triggers;
            END IF;
        END $$;
    """))
    logger.info("0036: validation passed")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------


def downgrade() -> None:
    # Remove extra-table triggers first (no policies to drop there).
    for extra_table in ["reservations", "restaurant_tables"]:
        op.execute(sa.text(
            f"DROP TRIGGER IF EXISTS trg_auto_org_location_{extra_table} "
            f"ON {extra_table}"
        ))
        logger.info("0036 downgrade: extra trigger removed from %s", extra_table)

    # Remove triggers and policies from RLS tables in reverse order.
    for table in reversed(_RLS_TABLES):
        trigger_name = f"trg_auto_org_location_{table}"
        op.execute(sa.text(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}"
        ))
        op.execute(sa.text(
            f"DROP POLICY IF EXISTS {_NEW_POLICY} ON {table}"
        ))
        logger.info("0036 downgrade: trigger + policy removed from %s", table)

    # Drop all four trigger functions.
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS _auto_populate_org_location_branch_only()"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS _auto_populate_org_location_branch()"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS _auto_populate_org_location()"
    ))
    op.execute(sa.text(
        "DROP FUNCTION IF EXISTS _auto_populate_org_only()"
    ))
    logger.info("0036 downgrade: trigger functions dropped")
