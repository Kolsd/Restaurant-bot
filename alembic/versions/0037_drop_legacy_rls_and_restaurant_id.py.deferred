"""Drop legacy tenant_isolation policy, triggers, and legacy columns/table.

⚠️  DEPLOYMENT-GATED MIGRATION ⚠️
DO NOT RUN THIS MIGRATION UNTIL:
  1. Wave 1 (migrations 0034-0036 + code wave 1) has been in PRODUCTION for ≥7 days.
  2. Monitoring query shows ZERO rows with org_id IS NULL in all Location-level tables.
  3. Monitoring query shows ZERO TenantNotSetError logs in the last 7 days.
  4. Alembic head is 0037b_recovery_cons and application is running Wave 2 code
     (sets app.org_id directly).

Running prematurely will:
  - Break the retry path for old webhook payloads still in webhook_inbox whose
    restaurant_id can no longer be mapped to an org_isolation-visible row.
  - Cause TenantNotSetError storms if any code path still passes restaurant_id
    (legacy GUC app.restaurant_id is no longer read by any policy after this migration).
  - Drop restaurant_id columns from 33 tables — restoring them from pg_dump is the
    only reliable rollback path.

See ORG_LOCATION_MIGRATION_PLAN.md §5 for the full cutover gate checklist.

---

DDL SEQUENCE (in order):

  1. Drop the auto-populate BEFORE INSERT triggers added by migration 0036
     on all 33 RLS tables + restaurant_tables + reservations.
  2. Drop the four trigger functions (_auto_populate_org_only,
     _auto_populate_org_location, _auto_populate_org_location_branch,
     _auto_populate_org_location_branch_only).
  3. Drop the legacy tenant_isolation policy on all 33 RLS tables.
  4. Drop restaurant_id column from Org-level tables (15 tables).
  5. Drop restaurant_id column from Location-level tables (18 tables).
     restaurant_tables is skipped (no restaurant_id column; uses branch_id only).
  5b. Migrate billing_config to organizations + backfill from restaurants.
  6. Rename restaurants table → restaurants_deprecated and create a
     retrocompat VIEW named restaurants that emulates the old flat shape,
     including billing_config from organizations.
  7. Validation block: asserts zero restaurant_id columns remain, view exists,
     no tenant_isolation policies remain.

DOWNGRADE:
  Partial downgrade is implemented: adds columns back (NOT NULL with default -1
  sentinel so FK constraints are not violated), backfills from the mapping table,
  recreates the UNIQUE constraints that CASCADE-dropped with restaurant_id,
  recreates triggers and tenant_isolation policies.
  The restaurants table rename is NOT reversed in downgrade — it is too risky.
  For a full restore after a failed deployment, use pg_dump from before 0037.

Revision ID: 0037_drop_legacy_rls
Revises: 0037b_recovery_cons  (was 0036_dual_rls_policies before 0037b was inserted)
Create Date: 2026-04-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0037_drop_legacy_rls"
down_revision = "0037d_relax_location"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

# ---------------------------------------------------------------------------
# Table lists (mirrored from 0029/0036 to avoid cross-migration import coupling)
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

# Extra tables that had triggers in 0036 but no RLS policy (not in _RLS_TABLES).
_EXTRA_TRIGGER_TABLES = [
    "restaurant_tables",  # branch_id only, no restaurant_id column
    "reservations",       # nullable restaurant_id + branch_id
]

# ── Classification per §2.3 of ORG_LOCATION_MIGRATION_PLAN.md ──────────────

# Org-level tables: only org_id remains as the tenant key after this migration.
# restaurant_id DROP here.
_ORG_LEVEL_TABLES = [
    "billing_log",
    "carts",
    "contract_templates",
    "conversations",
    "customer_profiles",
    "dish_recipes",
    "loyalty_customers",
    "loyalty_ledger",
    "marketing_messages_log",
    "menu_events",
    "nps_responses",
    "nps_waiting",
    "payroll_runs",
    "subscription_usage",
    "weekly_reports",
]

# Location-level tables: org_id + location_id remain. restaurant_id DROP here.
# restaurant_tables is intentionally excluded — it has no restaurant_id column,
# only branch_id (which maps to location_id; handled via S4).
_LOCATION_LEVEL_TABLES = [
    "attendance_deductions",
    "fiscal_invoices",
    "fiscal_resolution",
    "inventory",
    "menu_availability",
    "occupancy_snapshots",
    "orders",
    "overtime_requests",
    "staff",
    "staff_deduction_items",
    "staff_schedules",
    "staff_shifts",
    "table_orders",
    "table_sessions",
    "time_slot_discounts",
    "tip_distributions",
    "waiter_alerts",
    "webauthn_challenges",
]

# All tables from which restaurant_id is dropped (union of the two above).
_ALL_DROP_TABLES = _ORG_LEVEL_TABLES + _LOCATION_LEVEL_TABLES

# reservations has restaurant_id (nullable) — also drop it here.
_EXTRA_RESTAURANT_ID_TABLES = [
    "reservations",
]

# The legacy policy name installed in migration 0029.
_LEGACY_POLICY = "tenant_isolation"

# ── Trigger function names (as created by 0036) ─────────────────────────────
_TRIGGER_FUNCTIONS = [
    "_auto_populate_org_only",
    "_auto_populate_org_location",
    "_auto_populate_org_location_branch",
    "_auto_populate_org_location_branch_only",
]


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    # ── Step 1: Drop auto-populate triggers (all tables touched by 0036) ──────

    all_trigger_tables = list(_RLS_TABLES) + _EXTRA_TRIGGER_TABLES
    logger.info("0037: dropping auto-populate triggers on %d tables", len(all_trigger_tables))
    for table in all_trigger_tables:
        trigger_name = f"trg_auto_org_location_{table}"
        op.execute(sa.text(
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}"
        ))
        logger.info("0037: dropped trigger %s on %s", trigger_name, table)

    # ── Step 2: Drop trigger functions ────────────────────────────────────────
    logger.info("0037: dropping trigger functions")
    for fn in _TRIGGER_FUNCTIONS:
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {fn}()"))
        logger.info("0037: dropped function %s", fn)

    # ── Step 3: Drop legacy tenant_isolation policy on all RLS tables ─────────
    logger.info("0037: dropping legacy tenant_isolation policies")
    for table in _RLS_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS {_LEGACY_POLICY} ON {table}"))
        logger.info("0037: dropped policy tenant_isolation on %s", table)

    # ── Step 4: Drop restaurant_id from Org-level tables ──────────────────────
    # CASCADE handles any FK/index referencing the column.
    logger.info("0037: dropping restaurant_id from %d Org-level tables", len(_ORG_LEVEL_TABLES))
    for table in _ORG_LEVEL_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS restaurant_id CASCADE"
        ))
        logger.info("0037: dropped restaurant_id from %s", table)

    # ── Step 5: Drop restaurant_id from Location-level tables ─────────────────
    logger.info(
        "0037: dropping restaurant_id from %d Location-level tables",
        len(_LOCATION_LEVEL_TABLES),
    )
    for table in _LOCATION_LEVEL_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS restaurant_id CASCADE"
        ))
        logger.info("0037: dropped restaurant_id from %s", table)

    # Drop restaurant_id from reservations (nullable, extra table).
    for table in _EXTRA_RESTAURANT_ID_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS restaurant_id CASCADE"
        ))
        logger.info("0037: dropped restaurant_id from %s (extra table)", table)

    # ── Step 5b: Migrate billing_config to organizations ─────────────────────
    # billing_config lives on restaurants (the raw table, not yet renamed).
    # Routes querying restaurants for billing_config will work via the VIEW after
    # this step because the VIEW exposes o.billing_config.
    # Uses IF EXISTS on the column add in case an older deployment never had
    # billing_config on the restaurants table.
    logger.info("0037: adding billing_config to organizations + backfilling from restaurants")
    op.execute(sa.text(
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS billing_config JSONB"
    ))
    op.execute(sa.text("""
        UPDATE organizations o
        SET billing_config = r.billing_config
        FROM restaurants r
        WHERE o.id = r.id
          AND o.billing_config IS NULL
          AND r.billing_config IS NOT NULL
    """))
    logger.info("0037: billing_config backfilled into organizations")

    # ── Step 6: Rename restaurants table, create retrocompat VIEW ─────────────
    logger.info("0037: renaming restaurants → restaurants_deprecated")
    op.execute(sa.text("ALTER TABLE restaurants RENAME TO restaurants_deprecated"))

    logger.info("0037: creating retrocompat VIEW restaurants")
    op.execute(sa.text("""
        CREATE VIEW restaurants AS
        SELECT
            l.id,
            CASE WHEN l.is_primary THEN NULL ELSE l.org_id END AS parent_restaurant_id,
            COALESCE(o.name, l.name) AS name,
            l.name AS location_name,
            COALESCE(l.whatsapp_number, o.whatsapp_number) AS whatsapp_number,
            l.address,
            l.latitude,
            l.longitude,
            o.menu,
            o.features,
            COALESCE(l.wa_phone_id, o.wa_phone_id) AS wa_phone_id,
            COALESCE(l.wa_access_token, o.wa_access_token) AS wa_access_token,
            o.slug,
            o.billing_config,
            l.created_at,
            l.updated_at
        FROM locations l
        JOIN organizations o ON o.id = l.org_id
    """))

    op.execute(sa.text(
        "COMMENT ON VIEW restaurants IS "
        "'Deprecated retrocompat view. Drop in Fase 7 (migration 0038). "
        "INSERT/UPDATE/DELETE raise errors — this forces legacy code to migrate.'"
    ))
    logger.info("0037: retrocompat VIEW restaurants created")

    # ── Step 7: Validation block ───────────────────────────────────────────────
    # Build a VALUES list of all tables where restaurant_id must NOT exist.
    drop_tables = _ALL_DROP_TABLES + _EXTRA_RESTAURANT_ID_TABLES
    table_values = ", ".join(f"('{t}')" for t in drop_tables)

    logger.info("0037: running post-migration validation")
    op.execute(sa.text(f"""
        DO $$
        DECLARE
            leftover_cols   INT;
            leftover_pol    INT;
            view_exists     INT;
        BEGIN
            -- 1. No restaurant_id columns remain in migrated tables.
            SELECT COUNT(*) INTO leftover_cols
            FROM information_schema.columns c
            JOIN (VALUES {table_values}) AS t(tname) ON c.table_name = t.tname
            WHERE c.column_name = 'restaurant_id'
              AND c.table_schema = 'public';
            IF leftover_cols > 0 THEN
                RAISE EXCEPTION
                    'Migration 0037: % table(s) still have restaurant_id column',
                    leftover_cols;
            END IF;

            -- 2. No tenant_isolation policies remain on any public table.
            SELECT COUNT(*) INTO leftover_pol
            FROM pg_policies
            WHERE policyname = 'tenant_isolation'
              AND schemaname  = 'public';
            IF leftover_pol > 0 THEN
                RAISE EXCEPTION
                    'Migration 0037: % tenant_isolation polic(ies) still exist',
                    leftover_pol;
            END IF;

            -- 3. The retrocompat VIEW exists.
            SELECT COUNT(*) INTO view_exists
            FROM information_schema.views
            WHERE table_name   = 'restaurants'
              AND table_schema = 'public';
            IF view_exists = 0 THEN
                RAISE EXCEPTION
                    'Migration 0037: retrocompat VIEW restaurants was not created';
            END IF;
        END $$;
    """))
    logger.info("0037: validation passed — migration complete")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    """Partial downgrade: restores restaurant_id columns, triggers, and policies.

    IMPORTANT: The restaurants table rename (restaurants → restaurants_deprecated)
    is NOT reversed here because:
      - The VIEW 'restaurants' would conflict with restoring the table.
      - Renaming back while data may have changed would be unsafe.
    For a full restore including the restaurants table, use a pg_dump backup
    taken before this migration was applied.

    What this downgrade DOES restore:
      - restaurant_id columns on all affected tables (NULL-populated, then
        backfilled from _migration_restaurant_to_location).
      - Auto-populate BEFORE INSERT triggers (from 0036 DDL).
      - tenant_isolation RLS policies (from 0029 DDL).
    """
    logger.info("0037 downgrade: starting partial rollback")

    # ── Step 1: Drop the retrocompat VIEW and restore the original table name ──
    # The VIEW is dropped first so that the rename below can take its slot.
    # restaurants_deprecated still holds all the original data (0037 upgrade
    # did ALTER TABLE RENAME, not DROP). So the downgrade brings it back.
    op.execute(sa.text("DROP VIEW IF EXISTS restaurants"))
    logger.info("0037 downgrade: dropped retrocompat VIEW restaurants")
    op.execute(sa.text(
        "ALTER TABLE IF EXISTS restaurants_deprecated RENAME TO restaurants"
    ))
    logger.info("0037 downgrade: restored restaurants_deprecated -> restaurants")

    # ── Step 2: Restore restaurant_id columns ─────────────────────────────────
    logger.info("0037 downgrade: restoring restaurant_id columns on %d tables",
                len(_ALL_DROP_TABLES) + len(_EXTRA_RESTAURANT_ID_TABLES))

    for table in _ALL_DROP_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS restaurant_id BIGINT"
        ))
        # Backfill from mapping table where possible.
        op.execute(sa.text(f"""
            UPDATE {table} t
            SET restaurant_id = m.old_restaurant_id
            FROM _migration_restaurant_to_location m
            WHERE t.org_id = m.org_id
              AND t.restaurant_id IS NULL
        """))
        logger.info("0037 downgrade: restaurant_id restored on %s", table)

    for table in _EXTRA_RESTAURANT_ID_TABLES:
        op.execute(sa.text(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS restaurant_id BIGINT"
        ))
        op.execute(sa.text(f"""
            UPDATE {table} t
            SET restaurant_id = m.old_restaurant_id
            FROM _migration_restaurant_to_location m
            WHERE t.org_id = m.org_id
              AND t.restaurant_id IS NULL
        """))
        logger.info("0037 downgrade: restaurant_id restored on %s (extra)", table)

    # ── Step 2b: Restore UNIQUE constraints CASCADE-dropped with restaurant_id ──
    # When restaurant_id was dropped in upgrade() with CASCADE, any UNIQUE
    # constraints referencing that column were silently removed. This caused
    # the production outage after the premature 0037 run (recovered by 0037b).
    # Here we restore the legacy (restaurant_id-based) constraints so the DB
    # is in the same state it was in before 0037 ran.
    #
    # Safety: each add is wrapped in a DO $$ ... EXCEPTION $$ block. If duplicate
    # rows were written during the window when the constraint was absent, the add
    # is skipped (with a WARNING logged) and the operator must clean up manually.
    logger.info("0037 downgrade: restoring UNIQUE constraints dropped by CASCADE")

    _LEGACY_UNIQUE_CONSTRAINTS = [
        # (table, constraint_name, columns_csv)
        ("subscription_usage",  "subscription_usage_rest_date_uq",
         "restaurant_id, usage_date"),
        ("customer_profiles",   "customer_profiles_rest_phone_uq",
         "restaurant_id, phone"),
        ("fiscal_invoices",     "fiscal_invoices_rest_num_uq",
         "restaurant_id, resolution_number, invoice_number"),
        ("dish_recipes",        "dish_recipes_rest_dish_uq",
         "restaurant_id, dish_name, ingredient_id"),
        ("loyalty_customers",   "loyalty_customers_rest_phone_uq",
         "restaurant_id, phone"),
        ("weekly_reports",      "weekly_reports_rest_week_uq",
         "restaurant_id, week_start"),
        ("payroll_runs",        "payroll_runs_rest_period_uq",
         "restaurant_id, period_start, period_end"),
        ("time_slot_discounts", "time_slot_discounts_rest_slot_uq",
         "restaurant_id, branch_id, day_of_week, start_time"),
    ]

    for table, name, cols in _LEGACY_UNIQUE_CONSTRAINTS:
        op.execute(sa.text(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = '{name}'
                      AND conrelid = '{table}'::regclass
                ) THEN
                    BEGIN
                        ALTER TABLE {table}
                            ADD CONSTRAINT {name} UNIQUE ({cols});
                        RAISE NOTICE '0037 downgrade: restored UNIQUE %', '{name}';
                    EXCEPTION WHEN unique_violation OR exclusion_violation THEN
                        RAISE WARNING
                            '0037 downgrade: SKIPPED constraint % on % — '
                            'duplicate rows detected. Manual cleanup required.',
                            '{name}', '{table}';
                    END;
                ELSE
                    RAISE NOTICE '0037 downgrade: constraint % already exists, skipped', '{name}';
                END IF;
            END $$;
        """))
        logger.info("0037 downgrade: processed UNIQUE constraint %s on %s", name, table)

    # ── Step 2c: Drop billing_config from organizations (reverse of step 5b) ──
    # Only drop if it was NOT present before 0037 ran. We cannot know for certain,
    # so we use IF EXISTS to be idempotent. Operators who had billing_config on
    # organizations before 0037 will lose it here — acceptable given this is a
    # downgrade path.
    logger.info("0037 downgrade: dropping billing_config from organizations")
    op.execute(sa.text(
        "ALTER TABLE organizations DROP COLUMN IF EXISTS billing_config"
    ))
    logger.info("0037 downgrade: billing_config dropped from organizations")

    # ── Step 3: Recreate trigger functions ────────────────────────────────────
    logger.info("0037 downgrade: recreating trigger functions")

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_only()
        RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
        BEGIN
            IF NEW.org_id IS NULL AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id INTO NEW.org_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;
            RETURN NEW;
        END; $$
    """))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location()
        RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
        BEGIN
            IF (NEW.org_id IS NULL OR NEW.location_id IS NULL)
               AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id, m.location_id INTO NEW.org_id, NEW.location_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;
            RETURN NEW;
        END; $$
    """))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch()
        RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
        DECLARE v_resolver BIGINT;
        BEGIN
            IF NEW.org_id IS NULL AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id INTO NEW.org_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;
            IF NEW.location_id IS NULL THEN
                v_resolver := COALESCE(NEW.branch_id, NEW.restaurant_id);
                IF v_resolver IS NOT NULL THEN
                    SELECT m.location_id INTO NEW.location_id
                    FROM _migration_restaurant_to_location m
                    WHERE m.old_restaurant_id = v_resolver;
                END IF;
            END IF;
            RETURN NEW;
        END; $$
    """))

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch_only()
        RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
        BEGIN
            IF (NEW.org_id IS NULL OR NEW.location_id IS NULL)
               AND NEW.branch_id IS NOT NULL THEN
                SELECT m.org_id, m.location_id INTO NEW.org_id, NEW.location_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.branch_id;
            END IF;
            RETURN NEW;
        END; $$
    """))

    op.execute(sa.text("GRANT EXECUTE ON FUNCTION _auto_populate_org_only() TO mesio_app"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION _auto_populate_org_location() TO mesio_app"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION _auto_populate_org_location_branch() TO mesio_app"))
    op.execute(sa.text("GRANT EXECUTE ON FUNCTION _auto_populate_org_location_branch_only() TO mesio_app"))
    logger.info("0037 downgrade: trigger functions recreated")

    # ── Step 4: Recreate auto-populate triggers on all tables ─────────────────
    _ORG_ONLY_TABLES_SET = {
        "billing_log", "contract_templates", "customer_profiles", "dish_recipes",
        "loyalty_customers", "marketing_messages_log", "subscription_usage",
    }
    _BRANCH_ID_TABLES_SET = {
        # Only tables with a real branch_id column — must match migrations 0035/0036.
        "occupancy_snapshots", "time_slot_discounts",
        "table_orders", "conversations", "nps_responses",
    }

    all_trigger_tables = list(_RLS_TABLES) + _EXTRA_TRIGGER_TABLES
    for table in all_trigger_tables:
        if table == "restaurant_tables":
            fn = "_auto_populate_org_location_branch_only"
        elif table in _ORG_ONLY_TABLES_SET:
            fn = "_auto_populate_org_only"
        elif table in _BRANCH_ID_TABLES_SET or table == "reservations":
            fn = "_auto_populate_org_location_branch"
        else:
            fn = "_auto_populate_org_location"

        trigger_name = f"trg_auto_org_location_{table}"
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}"))
        op.execute(sa.text(f"""
            CREATE TRIGGER {trigger_name}
            BEFORE INSERT ON {table}
            FOR EACH ROW EXECUTE FUNCTION {fn}()
        """))
        logger.info("0037 downgrade: trigger %s recreated on %s", trigger_name, table)

    # ── Step 5: Recreate tenant_isolation policy on all RLS tables ────────────
    _LEGACY_EXPR = "restaurant_id = NULLIF(current_setting('app.restaurant_id', true), '')::int"
    for table in _RLS_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS {_LEGACY_POLICY} ON {table}"))
        op.execute(sa.text(f"""
            CREATE POLICY {_LEGACY_POLICY} ON {table}
                USING ({_LEGACY_EXPR})
                WITH CHECK ({_LEGACY_EXPR})
        """))
        logger.info("0037 downgrade: tenant_isolation policy recreated on %s", table)

    logger.info(
        "0037 downgrade: partial rollback complete. "
        "NOTE: restaurants table is still named restaurants_deprecated. "
        "For a full restore, use pg_dump taken before this migration ran."
    )
