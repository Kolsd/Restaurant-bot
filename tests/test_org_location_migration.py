"""
tests/test_org_location_migration.py

Integration tests for migrations 0034 (create organizations/locations tables
and backfill from restaurants) and 0035 (add org_id/location_id columns to
all tenant-scoped tables).

These tests run against a real PostgreSQL database. They are SKIPPED
automatically if no DATABASE_URL or TEST_DATABASE_URL is set — the suite
stays green in CI without a database.

Tests assert invariants that should hold AFTER alembic upgrade head:
  1. test_every_matriz_has_one_org          — 1:1 Matriz → Org
  2. test_every_restaurant_has_one_location — 1:1 restaurant → Location
  3. test_mapping_table_covers_all_restaurants — mapping is complete
  4. test_primary_location_unique_per_org   — unique partial index holds
  5. test_orphan_org_ids                    — no NOT-NULL org_id column is NULL
     (checks orders, staff, inventory, table_orders, billing_log, conversations)
  6. test_preflight_no_duplicate_sucursal_names_under_same_parent — PREFLIGHT
     (informational warning — must run on pre-migration DB state)

The `db_conn` fixture (defined in conftest.py) wraps each test in a rolled-back
transaction for isolation. These tests are purely READ-ONLY against the post-
migration state; they don't insert data.
"""

import logging

import pytest

# OBSOLETE post-0038 (2026-04-19). Originally validated transitional invariants
# of the Wave-2 migration (0034 backfill + 0035 column adds). The artifacts
# they assert on — `_migration_restaurant_to_location` mapping table,
# `is_primary` column, dual restaurant_id/org_id columns — were all dropped
# in migration 0038. The migrations they validate already shipped successfully.
# Module-level skip preserves git history without polluting CI runs.
pytestmark = pytest.mark.skip(
    reason="Tests for transitional Wave-2 migration state. Artifacts dropped in 0038. "
           "Migration 0034/0035 invariants are historical — no longer assertable on current schema."
)

_preflight_logger = logging.getLogger("mesio.migration.preflight")


# ── Test 1: every Matriz (parent_restaurant_id IS NULL) has exactly one Org ──

@pytest.mark.asyncio
async def test_every_matriz_has_one_org(db_conn):
    """
    For each row in `restaurants` with parent_restaurant_id IS NULL,
    there must be exactly one corresponding row in `organizations` with the
    same id.
    """
    # Count Matrizes
    matriz_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM restaurants WHERE parent_restaurant_id IS NULL"
    )

    if matriz_count == 0:
        pytest.skip("No Matriz rows in restaurants — nothing to assert")

    # Count Organizations that match a Matriz id
    matched_count = await db_conn.fetchval(
        """
        SELECT COUNT(*)
        FROM restaurants r
        JOIN organizations o ON o.id = r.id
        WHERE r.parent_restaurant_id IS NULL
        """
    )

    assert matched_count == matriz_count, (
        f"Expected {matriz_count} organizations matching Matrizes, "
        f"but only {matched_count} match"
    )

    # No duplicate orgs (there should be exactly one org per Matriz id)
    dup_count = await db_conn.fetchval(
        """
        SELECT COUNT(*) FROM (
            SELECT id, COUNT(*) AS c
            FROM organizations
            GROUP BY id
            HAVING COUNT(*) > 1
        ) dups
        """
    )
    assert dup_count == 0, f"Found {dup_count} duplicate org ids"


# ── Test 2: every restaurant (Matriz + Sucursal) has exactly one Location ────

@pytest.mark.asyncio
async def test_every_restaurant_has_one_location(db_conn):
    """
    For each row in `restaurants`, there must be exactly one corresponding
    row in `locations` pointed to by `_migration_restaurant_to_location`.
    """
    total_restaurants = await db_conn.fetchval(
        "SELECT COUNT(*) FROM restaurants"
    )

    if total_restaurants == 0:
        pytest.skip("No rows in restaurants — nothing to assert")

    # Every restaurant must appear in the mapping, and mapping must point to
    # a valid location
    restaurants_with_location = await db_conn.fetchval(
        """
        SELECT COUNT(*)
        FROM restaurants r
        JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
        JOIN locations l ON l.id = m.location_id
        """
    )

    assert restaurants_with_location == total_restaurants, (
        f"Expected {total_restaurants} restaurants each with 1 location, "
        f"but only {restaurants_with_location} have one"
    )

    # Verify Matriz rows (is_primary = true) are in locations
    matriz_primary_count = await db_conn.fetchval(
        """
        SELECT COUNT(*)
        FROM restaurants r
        JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
        JOIN locations l ON l.id = m.location_id AND l.is_primary = true
        WHERE r.parent_restaurant_id IS NULL
        """
    )
    matriz_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM restaurants WHERE parent_restaurant_id IS NULL"
    )
    assert matriz_primary_count == matriz_count, (
        f"Expected {matriz_count} primary locations for Matrizes, "
        f"got {matriz_primary_count}"
    )

    # Verify Sucursal rows (is_primary = false) are in locations
    sucursal_non_primary_count = await db_conn.fetchval(
        """
        SELECT COUNT(*)
        FROM restaurants r
        JOIN _migration_restaurant_to_location m ON m.old_restaurant_id = r.id
        JOIN locations l ON l.id = m.location_id AND l.is_primary = false
        WHERE r.parent_restaurant_id IS NOT NULL
        """
    )
    sucursal_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM restaurants WHERE parent_restaurant_id IS NOT NULL"
    )
    assert sucursal_non_primary_count == sucursal_count, (
        f"Expected {sucursal_count} non-primary locations for Sucursales, "
        f"got {sucursal_non_primary_count}"
    )


# ── Test 3: mapping table covers all restaurants ──────────────────────────────

@pytest.mark.asyncio
async def test_mapping_table_covers_all_restaurants(db_conn):
    """
    `_migration_restaurant_to_location` must have exactly one row per
    restaurant, with no NULLs in org_id or location_id.
    """
    total_restaurants = await db_conn.fetchval(
        "SELECT COUNT(*) FROM restaurants"
    )

    if total_restaurants == 0:
        pytest.skip("No rows in restaurants — nothing to assert")

    # Check row count matches
    mapping_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM _migration_restaurant_to_location"
    )
    assert mapping_count == total_restaurants, (
        f"Expected {total_restaurants} mapping rows (one per restaurant), "
        f"got {mapping_count}"
    )

    # No NULL org_id in mapping
    null_org_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM _migration_restaurant_to_location WHERE org_id IS NULL"
    )
    assert null_org_count == 0, (
        f"Found {null_org_count} mapping rows with NULL org_id"
    )

    # No NULL location_id in mapping
    null_loc_count = await db_conn.fetchval(
        "SELECT COUNT(*)"
        " FROM _migration_restaurant_to_location WHERE location_id IS NULL"
    )
    assert null_loc_count == 0, (
        f"Found {null_loc_count} mapping rows with NULL location_id"
    )

    # No orphan restaurants (restaurant has no mapping row)
    orphan_count = await db_conn.fetchval(
        """
        SELECT COUNT(*)
        FROM restaurants r
        LEFT JOIN _migration_restaurant_to_location m
            ON m.old_restaurant_id = r.id
        WHERE m.old_restaurant_id IS NULL
        """
    )
    assert orphan_count == 0, (
        f"Found {orphan_count} restaurant(s) without a mapping row"
    )


# ── Test 4: unique partial index — exactly 1 primary location per org ─────────

@pytest.mark.asyncio
async def test_primary_location_unique_per_org(db_conn):
    """
    The unique partial index ux_locations_org_primary enforces that each
    Organization has at most one primary Location (is_primary = true).

    Query §12 from ORG_LOCATION_MIGRATION_PLAN.md:
      SELECT org_id, COUNT(*) FROM locations
      WHERE is_primary = true
      GROUP BY org_id HAVING COUNT(*) <> 1;
    Should return 0 rows.
    """
    orgs_with_multiple_primaries = await db_conn.fetchval(
        """
        SELECT COUNT(*) FROM (
            SELECT org_id
            FROM locations
            WHERE is_primary = true
            GROUP BY org_id
            HAVING COUNT(*) <> 1
        ) bad_orgs
        """
    )
    assert orgs_with_multiple_primaries == 0, (
        f"Found {orgs_with_multiple_primaries} organization(s) with "
        f"!= 1 primary location"
    )

    # Every org should have EXACTLY one primary location (not zero, not two+)
    org_count = await db_conn.fetchval("SELECT COUNT(*) FROM organizations")
    primary_count = await db_conn.fetchval(
        "SELECT COUNT(*) FROM locations WHERE is_primary = true"
    )

    if org_count > 0:
        assert primary_count == org_count, (
            f"Expected {org_count} primary locations (one per org), "
            f"got {primary_count}"
        )


# ── Test 5: no org_id IS NULL in any NOT-NULL org_id column ──────────────────

@pytest.mark.asyncio
async def test_orphan_org_ids(db_conn):
    """
    After migration 0035, every table that received org_id NOT NULL must have
    zero NULL values. This is the §12 'orphan_org_ids' query from the plan.

    We test a representative subset covering all classification tiers:
      - Location-level, NOT NULL location_id: orders, staff, inventory,
        table_orders, restaurant_tables
      - Org-level (no location_id): billing_log, subscription_usage,
        contract_templates, loyalty_customers
      - Org-level, nullable location_id: conversations

    NOTE: reservation_deposits and other tables are also checked but this
    subset covers all classification branches.
    """
    # Tables that MUST have org_id NOT NULL (all tables that got the column)
    tables_requiring_org_id = [
        # Location-level
        "orders",
        "staff",
        "inventory",
        "table_orders",
        "restaurant_tables",
        "attendance_deductions",
        "fiscal_invoices",
        "fiscal_resolution",
        "menu_availability",
        "occupancy_snapshots",
        "overtime_requests",
        "staff_deduction_items",
        "staff_schedules",
        "staff_shifts",
        "table_sessions",
        "time_slot_discounts",
        "waiter_alerts",
        "webauthn_challenges",
        # Org-level
        "billing_log",
        "contract_templates",
        "customer_profiles",
        "dish_recipes",
        "loyalty_customers",
        "marketing_messages_log",
        "subscription_usage",
        # Org-level with nullable location_id
        "conversations",
        "loyalty_ledger",
        "menu_events",
        "nps_responses",
        "nps_waiting",
        "payroll_runs",
        "weekly_reports",
        "carts",
        # Extra tables
        "reservation_deposits",
        "reservations",
    ]

    for table in tables_requiring_org_id:
        # Check the column exists first (skip if migration hasn't been applied)
        col_exists = await db_conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = $1
              AND column_name  = 'org_id'
            """,
            table,
        )
        if not col_exists:
            pytest.fail(
                f"Table '{table}' is missing org_id column — "
                f"migration 0035 may not have run"
            )

        null_count = await db_conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE org_id IS NULL"
        )
        assert null_count == 0, (
            f"Table '{table}' has {null_count} rows with org_id IS NULL "
            f"after migration"
        )

    # Tables that MUST have location_id NOT NULL (Location-level, non-nullable)
    tables_requiring_location_id = [
        "orders",
        "staff",
        "inventory",
        "table_orders",
        "restaurant_tables",
        "attendance_deductions",
        "fiscal_invoices",
        "fiscal_resolution",
        "menu_availability",
        "occupancy_snapshots",
        "overtime_requests",
        "staff_deduction_items",
        "staff_schedules",
        "staff_shifts",
        "table_sessions",
        "time_slot_discounts",
        "waiter_alerts",
        "webauthn_challenges",
        "reservation_deposits",
    ]

    for table in tables_requiring_location_id:
        col_exists = await db_conn.fetchval(
            """
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = $1
              AND column_name  = 'location_id'
            """,
            table,
        )
        if not col_exists:
            pytest.fail(
                f"Table '{table}' is missing location_id column — "
                f"migration 0035 may not have run"
            )

        null_count = await db_conn.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE location_id IS NULL"
        )
        assert null_count == 0, (
            f"Table '{table}' has {null_count} rows with location_id IS NULL "
            f"after migration (this table requires NOT NULL location_id)"
        )


# ── Preflight Test 6: duplicate sucursal names under same parent ──────────────

@pytest.mark.asyncio
async def test_preflight_no_duplicate_sucursal_names_under_same_parent(db_conn):
    """
    PREFLIGHT — run on the pre-migration DB state (before 0034 is applied).

    This test is informational: it WARNS if any Matriz has two or more
    Sucursales with exactly the same name. With the legacy_restaurant_id fix
    in 0034 this is no longer a correctness risk, but duplicate names are
    still worth flagging for operators (they confuse dashboard UX and reports).

    This test never fails — it only logs warnings. The operator can review
    the output and rename Sucursales before running the migration if desired.
    """
    rows = await db_conn.fetch(
        """
        SELECT parent_restaurant_id, name, COUNT(*) AS cnt
        FROM restaurants
        WHERE parent_restaurant_id IS NOT NULL
        GROUP BY parent_restaurant_id, name
        HAVING COUNT(*) > 1
        ORDER BY parent_restaurant_id, name
        """
    )

    if not rows:
        _preflight_logger.info(
            "preflight: no duplicate sucursal names found — clean migration"
        )
        return

    # Log each duplicate as a warning (informational, does not fail the test)
    for row in rows:
        _preflight_logger.warning(
            "preflight: parent_restaurant_id=%s has %d sucursales named %r — "
            "consider renaming before migration for cleaner dashboard UX",
            row["parent_restaurant_id"],
            row["cnt"],
            row["name"],
        )

    total_dupes = sum(r["cnt"] for r in rows)
    _preflight_logger.warning(
        "preflight: %d duplicate sucursal name(s) across %d group(s) detected. "
        "Migration 0034 is safe (uses legacy_restaurant_id), but review recommended.",
        total_dupes,
        len(rows),
    )
    # Intentionally no assertion — this is a warning-only preflight check.
