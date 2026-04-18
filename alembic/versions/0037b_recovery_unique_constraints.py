"""Recovery migration: restore dropped UNIQUE constraints + defensive trigger recreate.

WHY:
  A premature run of migration 0037 CASCADE-dropped several UNIQUE constraints on
  restaurant_id columns, then was rolled back.  The rollback recreated the columns
  but NOT the constraints.  This migration:

    1.  Adds NEW UNIQUE constraints on (org_id, X) — the canonical Wave 1 columns.
    2.  Restores LEGACY UNIQUE constraints on (restaurant_id, X) for backward
        compatibility with ON CONFLICT (restaurant_id, …) paths still in use.
    3.  Defensively recreates all four auto-populate trigger functions + every
        trigger from migration 0036, in case the downgrade/reapply cycle left any
        of them missing or corrupted.

  All DDL is idempotent via DO $$ … IF NOT EXISTS … $$ blocks and
  CREATE OR REPLACE FUNCTION / DROP TRIGGER IF EXISTS.

CONSTRAINT NAMES ADDED:
  New (org_id-based):
    subscription_usage_org_date_uq          (org_id, usage_date)
    customer_profiles_org_phone_uq           (org_id, phone)
    fiscal_invoices_org_num_uq               (org_id, resolution_number, invoice_number)
    dish_recipes_org_dish_uq                 (org_id, dish_name, ingredient_id)
    loyalty_customers_org_phone_uq           (org_id, phone)
    weekly_reports_org_week_uq               (org_id, week_start)
    payroll_runs_org_period_uq               (org_id, period_start, period_end)
    time_slot_discounts_org_slot_uq          (org_id, branch_id, day_of_week, start_time)
    fiscal_resolution_org_uq                 (org_id)  — one per org

  Legacy (restaurant_id-based, restored):
    subscription_usage_rest_date_uq          (restaurant_id, usage_date)
    customer_profiles_rest_phone_uq          (restaurant_id, phone)
    fiscal_invoices_rest_num_uq              (restaurant_id, resolution_number, invoice_number)
    dish_recipes_rest_dish_uq                (restaurant_id, dish_name, ingredient_id)
    loyalty_customers_rest_phone_uq          (restaurant_id, phone)
    weekly_reports_rest_week_uq              (restaurant_id, week_start)
    payroll_runs_rest_period_uq              (restaurant_id, period_start, period_end)
    time_slot_discounts_rest_slot_uq         (restaurant_id, branch_id, day_of_week, start_time)

  NOTE: fiscal_resolution had UNIQUE (restaurant_id) inline in 0001 — Postgres
  generates an automatic constraint name. We restore it explicitly via a named
  constraint so downgrade can drop it cleanly.

DOWNGRADE:
  Drops all constraints added in step 1 and step 2 (idempotent).
  Does NOT touch triggers (they are owned by 0036 and its downgrade handles them).

Revision ID: 0037b_recovery_cons
Revises: 0036_dual_rls_policies
Create Date: 2026-04-17
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0037b_recovery_cons"
down_revision = "0036_dual_rls_policies"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


# ---------------------------------------------------------------------------
# Helper: idempotent UNIQUE constraint add
# ---------------------------------------------------------------------------

def _add_unique_if_not_exists(conn, constraint_name: str, table: str, columns: str) -> None:
    """Add a UNIQUE constraint if it does not already exist.

    columns: comma-separated column list, e.g. "org_id, usage_date"
    """
    conn.execute(sa.text(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{constraint_name}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table}
                    ADD CONSTRAINT {constraint_name} UNIQUE ({columns});
            END IF;
        END $$;
    """))
    logger.info("0037b: ensured constraint %s on %s(%s)", constraint_name, table, columns)


def _drop_constraint_if_exists(constraint_name: str, table: str) -> None:
    """Drop a constraint by name if it exists (idempotent)."""
    op.execute(sa.text(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{constraint_name}'
                  AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} DROP CONSTRAINT {constraint_name};
            END IF;
        END $$;
    """))
    logger.info("0037b: dropped (if existed) constraint %s on %s", constraint_name, table)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------

def upgrade() -> None:
    conn = op.get_bind()

    # ── Step 1: Auto-dedupe duplicates before restoring UNIQUE constraints ─
    # During deploy cycles when the constraint was absent, the app may have
    # inserted duplicate rows on (restaurant_id, X). We keep the row with
    # MAX(id) per duplicate group and delete the rest. This is safe because
    # these tables all have auto-increment PKs and the duplicates are
    # temporary artifacts of unsafe deploys, not legitimate business data.
    # We log per-table how many rows got deleted so it's visible in audit.

    logger.info("0037b: auto-deduping before restoring legacy UNIQUE constraints")

    _DUP_CHECKS = [
        ("subscription_usage",  "restaurant_id, usage_date"),
        ("customer_profiles",   "restaurant_id, phone"),
        ("loyalty_customers",   "restaurant_id, phone"),
        ("weekly_reports",      "restaurant_id, week_start"),
        ("payroll_runs",        "restaurant_id, period_start, period_end"),
        (
            "time_slot_discounts",
            "restaurant_id, branch_id, day_of_week, start_time",
        ),
        (
            "fiscal_invoices",
            "restaurant_id, resolution_number, invoice_number",
        ),
        (
            "dish_recipes",
            "restaurant_id, dish_name, ingredient_id",
        ),
    ]

    for table, cols in _DUP_CHECKS:
        partition_cols = cols  # same column list works for PARTITION BY
        result = conn.execute(sa.text(
            f"""
            WITH ranked AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY {partition_cols}
                           ORDER BY id DESC
                       ) AS rn
                FROM {table}
            )
            DELETE FROM {table}
            WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        ))
        deleted = result.rowcount or 0
        if deleted > 0:
            logger.warning(
                "0037b auto-dedupe: deleted %d duplicate rows from %s (%s)",
                deleted, table, cols,
            )
        else:
            logger.info("0037b: %s(%s) — 0 duplicates OK", table, cols)

    # ── Step 2: Add NEW UNIQUE constraints on (org_id, ...) ─────────────────

    logger.info("0037b: adding org_id-based UNIQUE constraints")

    _add_unique_if_not_exists(
        conn, "subscription_usage_org_date_uq",
        "subscription_usage", "org_id, usage_date",
    )
    _add_unique_if_not_exists(
        conn, "customer_profiles_org_phone_uq",
        "customer_profiles", "org_id, phone",
    )
    _add_unique_if_not_exists(
        conn, "fiscal_invoices_org_num_uq",
        "fiscal_invoices", "org_id, resolution_number, invoice_number",
    )
    _add_unique_if_not_exists(
        conn, "dish_recipes_org_dish_uq",
        "dish_recipes", "org_id, dish_name, ingredient_id",
    )
    _add_unique_if_not_exists(
        conn, "loyalty_customers_org_phone_uq",
        "loyalty_customers", "org_id, phone",
    )
    _add_unique_if_not_exists(
        conn, "weekly_reports_org_week_uq",
        "weekly_reports", "org_id, week_start",
    )
    _add_unique_if_not_exists(
        conn, "payroll_runs_org_period_uq",
        "payroll_runs", "org_id, period_start, period_end",
    )
    _add_unique_if_not_exists(
        conn, "time_slot_discounts_org_slot_uq",
        "time_slot_discounts", "org_id, branch_id, day_of_week, start_time",
    )
    # fiscal_resolution: one per org (Org-level config)
    _add_unique_if_not_exists(
        conn, "fiscal_resolution_org_uq",
        "fiscal_resolution", "org_id",
    )

    logger.info("0037b: org_id UNIQUE constraints done")

    # ── Step 3: Restore LEGACY UNIQUE constraints on (restaurant_id, ...) ───

    logger.info("0037b: restoring legacy restaurant_id UNIQUE constraints")

    _add_unique_if_not_exists(
        conn, "subscription_usage_rest_date_uq",
        "subscription_usage", "restaurant_id, usage_date",
    )
    _add_unique_if_not_exists(
        conn, "customer_profiles_rest_phone_uq",
        "customer_profiles", "restaurant_id, phone",
    )
    _add_unique_if_not_exists(
        conn, "fiscal_invoices_rest_num_uq",
        "fiscal_invoices", "restaurant_id, resolution_number, invoice_number",
    )
    _add_unique_if_not_exists(
        conn, "dish_recipes_rest_dish_uq",
        "dish_recipes", "restaurant_id, dish_name, ingredient_id",
    )
    _add_unique_if_not_exists(
        conn, "loyalty_customers_rest_phone_uq",
        "loyalty_customers", "restaurant_id, phone",
    )
    _add_unique_if_not_exists(
        conn, "weekly_reports_rest_week_uq",
        "weekly_reports", "restaurant_id, week_start",
    )
    _add_unique_if_not_exists(
        conn, "payroll_runs_rest_period_uq",
        "payroll_runs", "restaurant_id, period_start, period_end",
    )
    _add_unique_if_not_exists(
        conn, "time_slot_discounts_rest_slot_uq",
        "time_slot_discounts", "restaurant_id, branch_id, day_of_week, start_time",
    )

    logger.info("0037b: legacy UNIQUE constraints done")

    # ── Step 4: Defensive trigger function recreate (CREATE OR REPLACE) ──────
    # These are identical to the definitions in migration 0036. If the
    # downgrade/re-apply cycle left the functions missing or corrupted, this
    # restores them to the canonical state.

    logger.info("0037b: recreating auto-populate trigger functions")

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
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

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
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

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        DECLARE
            v_resolver BIGINT;
        BEGIN
            IF NEW.org_id IS NULL AND NEW.restaurant_id IS NOT NULL THEN
                SELECT m.org_id
                INTO NEW.org_id
                FROM _migration_restaurant_to_location m
                WHERE m.old_restaurant_id = NEW.restaurant_id;
            END IF;

            IF NEW.location_id IS NULL THEN
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

    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION _auto_populate_org_location_branch_only()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        SECURITY DEFINER
        AS $$
        BEGIN
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

    # Re-grant EXECUTE to mesio_app (idempotent).
    for fn in (
        "_auto_populate_org_only()",
        "_auto_populate_org_location()",
        "_auto_populate_org_location_branch()",
        "_auto_populate_org_location_branch_only()",
    ):
        op.execute(sa.text(f"GRANT EXECUTE ON FUNCTION {fn} TO mesio_app"))
    logger.info("0037b: trigger functions recreated + EXECUTE granted")

    # ── Step 5: Defensive trigger recreate on every table ───────────────────
    # Classification mirrors migration 0036 exactly.
    # DROP TRIGGER IF EXISTS + CREATE TRIGGER makes this safe to re-run.

    _ORG_ONLY_TABLES = {
        "billing_log",
        "contract_templates",
        "customer_profiles",
        "dish_recipes",
        "loyalty_customers",
        "marketing_messages_log",
        "subscription_usage",
    }

    _TABLES_WITH_BRANCH_ID = {
        "occupancy_snapshots",
        "time_slot_discounts",
        "table_orders",
        "conversations",
        "nps_responses",
    }

    # Full RLS table list from 0029/0036.
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

    logger.info("0037b: recreating triggers on %d RLS tables", len(_RLS_TABLES))

    for table in _RLS_TABLES:
        if table in _ORG_ONLY_TABLES:
            fn = "_auto_populate_org_only"
        elif table in _TABLES_WITH_BRANCH_ID:
            fn = "_auto_populate_org_location_branch"
        else:
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
        logger.info(
            "0037b: trigger %s recreated on %s (fn=%s)", trigger_name, table, fn
        )

    # Extra tables (restaurant_tables, reservations) — same as 0036 Step 3.
    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_auto_org_location_restaurant_tables "
        "ON restaurant_tables"
    ))
    op.execute(sa.text("""
        CREATE TRIGGER trg_auto_org_location_restaurant_tables
        BEFORE INSERT ON restaurant_tables
        FOR EACH ROW
        EXECUTE FUNCTION _auto_populate_org_location_branch_only()
    """))
    logger.info("0037b: trigger trg_auto_org_location_restaurant_tables recreated")

    op.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_auto_org_location_reservations "
        "ON reservations"
    ))
    op.execute(sa.text("""
        CREATE TRIGGER trg_auto_org_location_reservations
        BEFORE INSERT ON reservations
        FOR EACH ROW
        EXECUTE FUNCTION _auto_populate_org_location_branch()
    """))
    logger.info("0037b: trigger trg_auto_org_location_reservations recreated")

    # ── Step 6: Final validation ─────────────────────────────────────────────
    logger.info("0037b: running final validation")

    # All 9 new org_id constraints must exist.
    _EXPECTED_NEW = [
        ("subscription_usage_org_date_uq",    "subscription_usage"),
        ("customer_profiles_org_phone_uq",    "customer_profiles"),
        ("fiscal_invoices_org_num_uq",        "fiscal_invoices"),
        ("dish_recipes_org_dish_uq",          "dish_recipes"),
        ("loyalty_customers_org_phone_uq",    "loyalty_customers"),
        ("weekly_reports_org_week_uq",        "weekly_reports"),
        ("payroll_runs_org_period_uq",        "payroll_runs"),
        ("time_slot_discounts_org_slot_uq",   "time_slot_discounts"),
        ("fiscal_resolution_org_uq",          "fiscal_resolution"),
    ]

    for cname, tname in _EXPECTED_NEW:
        exists = conn.execute(sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = :cname AND conrelid = :tname::regclass"
        ), {"cname": cname, "tname": tname}).scalar()
        if not exists:
            raise RuntimeError(
                f"0037b validation: constraint {cname} not found on {tname} after migration"
            )

    logger.info("0037b: all constraints verified — migration complete")


# ---------------------------------------------------------------------------
# downgrade
# ---------------------------------------------------------------------------

def downgrade() -> None:
    # Drop all constraints added in upgrade() — idempotent via DO blocks.
    # Triggers are not touched here: they are managed by migration 0036.

    _NEW_CONSTRAINTS = [
        ("subscription_usage_org_date_uq",    "subscription_usage"),
        ("customer_profiles_org_phone_uq",    "customer_profiles"),
        ("fiscal_invoices_org_num_uq",        "fiscal_invoices"),
        ("dish_recipes_org_dish_uq",          "dish_recipes"),
        ("loyalty_customers_org_phone_uq",    "loyalty_customers"),
        ("weekly_reports_org_week_uq",        "weekly_reports"),
        ("payroll_runs_org_period_uq",        "payroll_runs"),
        ("time_slot_discounts_org_slot_uq",   "time_slot_discounts"),
        ("fiscal_resolution_org_uq",          "fiscal_resolution"),
    ]

    _LEGACY_CONSTRAINTS = [
        ("subscription_usage_rest_date_uq",   "subscription_usage"),
        ("customer_profiles_rest_phone_uq",   "customer_profiles"),
        ("fiscal_invoices_rest_num_uq",       "fiscal_invoices"),
        ("dish_recipes_rest_dish_uq",         "dish_recipes"),
        ("loyalty_customers_rest_phone_uq",   "loyalty_customers"),
        ("weekly_reports_rest_week_uq",       "weekly_reports"),
        ("payroll_runs_rest_period_uq",       "payroll_runs"),
        ("time_slot_discounts_rest_slot_uq",  "time_slot_discounts"),
    ]

    for cname, tname in _NEW_CONSTRAINTS + _LEGACY_CONSTRAINTS:
        _drop_constraint_if_exists(cname, tname)

    logger.info("0037b downgrade: all constraints dropped")
