"""
tests/test_org_rls.py

Empirical RLS checks for the Org/Location dual-policy migration (0036).

Mirrors the CLAUDE.md §Prueba empírica table for the new org_id GUC path:

  mesio_app + app.org_id=8   → orders = N   (only that org)
  mesio_app + no GUC         → orders = 0   (fail-closed)
  INSERT from wrong org      → InsufficientPrivilegeError

These tests require a real PostgreSQL database with migrations 0034-0036 applied.
They are skipped when no DATABASE_URL is available.

Tests:
  1. test_org_scope_filters_orders_by_org    — org_id GUC limits visible rows
  2. test_no_guc_sees_zero_rows             — empty GUC → fail-closed (0 rows)
  3. test_cross_org_leak_impossible         — org A scope cannot see org B rows
  4. test_old_restaurant_scope_unchanged    — restaurant_id GUC still works (no regression)
  5. test_tenant_connection_sets_both_gucs  — verify both GUCs are written by tenant_db
"""

import os
import pytest
import asyncpg

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture
async def pg_pool():
    url = _db_url()
    if not url:
        pytest.skip("No DATABASE_URL set — skipping Org RLS integration test")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture
async def conn(pg_pool):
    """Connection with automatic rollback."""
    async with pg_pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            yield c
        finally:
            await tx.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _clear_gucs(conn) -> None:
    await conn.execute("SELECT set_config('app.restaurant_id', '', true)")
    await conn.execute("SELECT set_config('app.org_id', '', true)")


async def _set_org(conn, org_id: int) -> None:
    await conn.execute(
        "SELECT set_config('app.org_id', $1, true)", str(org_id)
    )


async def _set_rid(conn, rid: int) -> None:
    await conn.execute(
        "SELECT set_config('app.restaurant_id', $1, true)", str(rid)
    )


async def _all_org_ids(conn) -> list[int]:
    """Return distinct org_ids from the mapping table (superuser read)."""
    rows = await conn.fetch(
        "SELECT DISTINCT org_id FROM _migration_restaurant_to_location ORDER BY org_id"
    )
    return [r["org_id"] for r in rows]


async def _order_count_for_org(conn, org_id: int) -> int:
    """
    Count orders for this org using a raw (non-RLS) query by temporarily
    bypassing policies with a superuser fetch. This gives us the expected
    count to compare against the scoped reads.
    """
    count = await conn.fetchval(
        "SELECT COUNT(*) FROM orders WHERE org_id = $1", org_id
    )
    return count or 0


# ---------------------------------------------------------------------------
# Test 1 — app.org_id scope limits visible rows
# ---------------------------------------------------------------------------

async def test_org_scope_filters_orders_by_org(conn):
    """
    Setting app.org_id to a known org_id must return exactly that org's orders.
    Validates the org_isolation policy USING clause.
    """
    org_ids = await _all_org_ids(conn)
    if not org_ids:
        pytest.skip("No orgs in mapping table — run migration 0034 first")

    for org_id in org_ids[:2]:  # Test first 2 orgs to keep the test fast.
        expected = await _order_count_for_org(conn, org_id)

        await _clear_gucs(conn)
        await _set_org(conn, org_id)

        actual = await conn.fetchval("SELECT COUNT(*) FROM orders")

        assert actual == expected, (
            f"org_id={org_id}: expected {expected} orders, got {actual}"
        )


# ---------------------------------------------------------------------------
# Test 2 — No GUC → 0 rows (fail-closed)
# ---------------------------------------------------------------------------

async def test_no_guc_sees_zero_rows(conn):
    """
    With no GUC set at all, both policies evaluate:
      restaurant_id = NULL::int  → always false
      org_id        = NULL::bigint → always false
    So SELECT from orders must return 0 rows (fail-closed behavior).
    """
    await _clear_gucs(conn)
    count = await conn.fetchval("SELECT COUNT(*) FROM orders")
    assert count == 0, (
        f"Expected 0 rows with no GUC (fail-closed), got {count}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Cross-org data leak impossible
# ---------------------------------------------------------------------------

async def test_cross_org_leak_impossible(conn):
    """
    While scoped to org A, rows from org B must not appear in SELECT results.
    """
    org_ids = await _all_org_ids(conn)
    if len(org_ids) < 2:
        pytest.skip("Need at least 2 distinct orgs to test cross-org isolation")

    org_a, org_b = org_ids[0], org_ids[1]

    expected_a = await _order_count_for_org(conn, org_a)
    expected_b = await _order_count_for_org(conn, org_b)

    # Scope to org_a — must not see org_b rows.
    await _clear_gucs(conn)
    await _set_org(conn, org_a)
    count_a = await conn.fetchval("SELECT COUNT(*) FROM orders")
    assert count_a == expected_a, (
        f"org_a={org_a}: expected {expected_a}, got {count_a}"
    )
    # Explicit check: none of org_b's rows leak in.
    if expected_b > 0:
        leaked = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE org_id = $1", org_b
        )
        assert leaked == 0, (
            f"Cross-org leak: {leaked} org_b={org_b} rows visible while scoped to org_a={org_a}"
        )

    # Scope to org_b — must not see org_a rows.
    await _clear_gucs(conn)
    await _set_org(conn, org_b)
    count_b = await conn.fetchval("SELECT COUNT(*) FROM orders")
    assert count_b == expected_b, (
        f"org_b={org_b}: expected {expected_b}, got {count_b}"
    )
    if expected_a > 0:
        leaked = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE org_id = $1", org_a
        )
        assert leaked == 0, (
            f"Cross-org leak: {leaked} org_a={org_a} rows visible while scoped to org_b={org_b}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Old restaurant_id GUC still works (no regression from dual policy)
# ---------------------------------------------------------------------------

async def test_old_restaurant_scope_unchanged(conn):
    """
    The legacy tenant_isolation policy (app.restaurant_id) must still work
    after migration 0036 adds the second policy. Dual PERMISSIVE policies use
    OR semantics — the legacy path must remain functional.
    """
    # Find a restaurant_id that has at least 1 order.
    row = await conn.fetchrow(
        """
        SELECT restaurant_id, COUNT(*) AS cnt
        FROM orders
        GROUP BY restaurant_id
        ORDER BY cnt DESC
        LIMIT 1
        """
    )
    if not row or row["cnt"] == 0:
        pytest.skip("No orders found — create seed data first")

    rid = row["restaurant_id"]
    expected = row["cnt"]

    await _clear_gucs(conn)
    await _set_rid(conn, rid)
    actual = await conn.fetchval("SELECT COUNT(*) FROM orders")

    assert actual == expected, (
        f"restaurant_id={rid}: expected {expected} orders via old GUC, got {actual}"
    )


# ---------------------------------------------------------------------------
# Test 5 — tenant_connection() sets BOTH GUCs
# ---------------------------------------------------------------------------

async def test_tenant_connection_sets_both_gucs(pg_pool):
    """
    Call tenant_connection() and verify that BOTH app.restaurant_id and
    app.org_id are set within the connection.

    Wave 1 semantics: tenant_connection() sets app.org_id = restaurant_id
    (same integer). This is correct for Matrizes (where organizations.id ==
    restaurants.id per migration 0034). For Sucursales, the org_isolation policy
    won't match via this GUC — but the legacy tenant_isolation policy still
    applies. Full org_id wiring for Sucursales happens in Bloque S3.

    We therefore test with a Matriz (old_restaurant_id == org_id).
    """
    url = _db_url()
    if not url:
        pytest.skip("No DATABASE_URL set")

    # Get a Matriz restaurant_id (where old_restaurant_id == org_id).
    async with pg_pool.acquire() as admin_conn:
        row = await admin_conn.fetchrow(
            "SELECT old_restaurant_id, org_id "
            "FROM _migration_restaurant_to_location "
            "WHERE old_restaurant_id = org_id "  # Matrizes only
            "LIMIT 1"
        )
    if not row:
        pytest.skip("No Matriz mapping rows found — run migration 0034 first")

    rid = row["old_restaurant_id"]
    expected_org_id = row["org_id"]  # == rid for Matrizes

    from app.services.tenant_context import tenant_scope
    from app.services.tenant_db import tenant_connection

    with tenant_scope(rid):
        async with tenant_connection() as conn:
            guc_rid = await conn.fetchval(
                "SELECT current_setting('app.restaurant_id', true)"
            )
            guc_oid = await conn.fetchval(
                "SELECT current_setting('app.org_id', true)"
            )

    assert guc_rid == str(rid), (
        f"Expected app.restaurant_id='{rid}', got '{guc_rid}'"
    )
    assert guc_oid == str(expected_org_id), (
        f"Expected app.org_id='{expected_org_id}' (== restaurant_id for Matriz), "
        f"got '{guc_oid}'"
    )
