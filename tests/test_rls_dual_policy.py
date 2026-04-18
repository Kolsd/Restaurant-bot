"""
tests/test_rls_dual_policy.py

Integration tests for the dual RLS policy introduced in migration 0036.

These tests require a real PostgreSQL database with migrations 0034-0036 applied
and the mesio_app non-superuser role configured. They are skipped gracefully
when DATABASE_URL (or TEST_DATABASE_URL) is not set, so the normal CI suite
(which uses mocks) is unaffected.

Test coverage:
  1. test_old_guc_still_reads_rows      — app.restaurant_id path still works
  2. test_new_guc_reads_same_rows       — app.org_id returns same rows for Matriz
  3. test_both_guc_set_consistent       — tenant_connection() sets both GUCs, counts match
  4. test_trigger_populates_org_id      — INSERT with only restaurant_id fills org_id
  5. test_trigger_populates_location_id — INSERT with restaurant_id+branch_id fills location_id
  6. test_cross_tenant_insert_blocked   — wrong org_id / restaurant_id in INSERT is rejected

Prerequisites (one-time):
  CREATE ROLE mesio_app ...;
  GRANT mesio_superadmin TO mesio_app;
  (see CLAUDE.md §Roles Postgres)

All writes are done inside a rolled-back transaction so data is never persisted.
"""

import os
import pytest
import asyncpg

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Skip if no real DB is available
# ---------------------------------------------------------------------------

def _db_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")


@pytest.fixture
async def raw_pool():
    """Pool connecting with DATABASE_URL (superuser or mesio_app)."""
    url = _db_url()
    if not url:
        pytest.skip("No DATABASE_URL set — skipping dual RLS integration test")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    yield pool
    await pool.close()


@pytest.fixture
async def raw_conn(raw_pool):
    """Single connection, every test is fully rolled back."""
    async with raw_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
        finally:
            await tx.rollback()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _set_restaurant_id(conn, rid: int) -> None:
    """Set app.restaurant_id GUC for this transaction."""
    await conn.execute(
        "SELECT set_config('app.restaurant_id', $1, true)",
        str(rid),
    )


async def _set_org_id(conn, oid: int) -> None:
    """Set app.org_id GUC for this transaction."""
    await conn.execute(
        "SELECT set_config('app.org_id', $1, true)",
        str(oid),
    )


async def _unset_gucs(conn) -> None:
    """Clear both GUCs (simulate no active scope)."""
    await conn.execute("SELECT set_config('app.restaurant_id', '', true)")
    await conn.execute("SELECT set_config('app.org_id', '', true)")


async def _first_matrix_with_orders(conn):
    """
    Return (restaurant_id, org_id, order_count) for the first Matriz that has
    at least one order. Returns None if the DB has no suitable data.

    We use bypass (superuser-style) by setting both GUCs in a single transaction
    to read cross-tenant data for fixture resolution.
    """
    # Temporarily bypass to read mapping + counts
    rows = await conn.fetch(
        """
        SELECT m.old_restaurant_id, m.org_id, COUNT(o.id) AS cnt
        FROM _migration_restaurant_to_location m
        JOIN organizations org ON org.id = m.org_id
        LEFT JOIN orders o ON o.restaurant_id = m.old_restaurant_id
        WHERE m.org_id = m.old_restaurant_id   -- only Matrizes
        GROUP BY m.old_restaurant_id, m.org_id
        ORDER BY cnt DESC
        LIMIT 1
        """
    )
    if not rows:
        return None
    row = rows[0]
    return {"restaurant_id": row["old_restaurant_id"],
            "org_id": row["org_id"],
            "order_count": row["cnt"]}


# ---------------------------------------------------------------------------
# Test 1 — Old GUC (app.restaurant_id) still reads rows
# ---------------------------------------------------------------------------

async def test_old_guc_still_reads_rows(raw_conn):
    """
    Set app.restaurant_id; SELECT from orders should return only that tenant's
    rows (same as before migration 0036).
    """
    info = await _first_matrix_with_orders(raw_conn)
    if info is None or info["order_count"] == 0:
        pytest.skip("No orders found in DB — create seed data first")

    await _unset_gucs(raw_conn)
    await _set_restaurant_id(raw_conn, info["restaurant_id"])
    rows = await raw_conn.fetch("SELECT id FROM orders")
    assert len(rows) == info["order_count"], (
        f"Expected {info['order_count']} orders with restaurant_id GUC, "
        f"got {len(rows)}"
    )


# ---------------------------------------------------------------------------
# Test 2 — New GUC (app.org_id) reads the same rows for a Matriz
# ---------------------------------------------------------------------------

async def test_new_guc_reads_same_rows(raw_conn):
    """
    For a Matriz, org_id == restaurant_id. Setting app.org_id (and leaving
    app.restaurant_id empty) should return the same order count as the old path.
    The org_isolation policy (migration 0036) must be the active filter.
    """
    info = await _first_matrix_with_orders(raw_conn)
    if info is None or info["order_count"] == 0:
        pytest.skip("No orders found in DB — create seed data first")

    await _unset_gucs(raw_conn)
    await _set_org_id(raw_conn, info["org_id"])
    rows = await raw_conn.fetch("SELECT id FROM orders")
    assert len(rows) == info["order_count"], (
        f"Expected {info['order_count']} orders with org_id GUC, "
        f"got {len(rows)} (org_id={info['org_id']})"
    )


# ---------------------------------------------------------------------------
# Test 3 — tenant_connection() sets BOTH GUCs; counts are consistent
# ---------------------------------------------------------------------------

async def test_both_guc_set_consistent(raw_pool):
    """
    tenant_connection() (updated in Bloque S2) sets app.restaurant_id AND
    app.org_id. This test drives tenant_connection() and compares the order
    count with a raw superuser count to verify consistency.
    """
    url = _db_url()
    if not url:
        pytest.skip("No DATABASE_URL set")

    # Discover a restaurant_id with some orders using a raw superuser connection.
    async with raw_pool.acquire() as admin_conn:
        rows = await admin_conn.fetch(
            """
            SELECT restaurant_id, COUNT(*) AS cnt
            FROM orders
            GROUP BY restaurant_id
            ORDER BY cnt DESC
            LIMIT 1
            """
        )
    if not rows:
        pytest.skip("No orders found in DB")

    restaurant_id = rows[0]["restaurant_id"]
    expected_count = rows[0]["cnt"]

    # Now use tenant_connection() which will set both GUCs.
    from app.services.tenant_context import tenant_scope
    from app.services.tenant_db import tenant_connection

    with tenant_scope(restaurant_id):
        async with tenant_connection() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM orders")

    assert count == expected_count, (
        f"Expected {expected_count} orders via tenant_connection(), got {count} "
        f"(restaurant_id={restaurant_id})"
    )


# ---------------------------------------------------------------------------
# Test 4 — Trigger populates org_id on INSERT (only restaurant_id provided)
# ---------------------------------------------------------------------------

async def test_trigger_populates_org_id_on_insert(raw_conn):
    """
    INSERT a row into `conversations` with only restaurant_id set (no org_id).
    The BEFORE INSERT trigger (_auto_populate_org_location) must populate org_id
    from the mapping table before the row is stored.

    We use the `conversations` table because it is Org-level, has nullable
    location_id, and is safe to INSERT into with minimal required columns.
    """
    # Find a restaurant_id in the mapping so the trigger has something to resolve.
    row = await raw_conn.fetchrow(
        "SELECT old_restaurant_id, org_id FROM _migration_restaurant_to_location LIMIT 1"
    )
    if not row:
        pytest.skip("No mapping rows found — run migrations 0034 first")

    rid = row["old_restaurant_id"]
    expected_org_id = row["org_id"]

    # Set the GUC so RLS WITH CHECK passes for the org_isolation policy.
    await _set_org_id(raw_conn, expected_org_id)
    await _set_restaurant_id(raw_conn, rid)

    # Insert with explicit NULL org_id — trigger must fill it.
    inserted = await raw_conn.fetchrow(
        """
        INSERT INTO conversations
            (phone, bot_number, restaurant_id, org_id, history, created_at)
        VALUES
            ($1, $2, $3, NULL, '[]'::jsonb, NOW())
        RETURNING org_id
        """,
        "+573009999991", "+571000000001", rid,
    )

    assert inserted is not None, "INSERT returned no row"
    assert inserted["org_id"] == expected_org_id, (
        f"Trigger should have set org_id={expected_org_id}, "
        f"got {inserted['org_id']}"
    )


# ---------------------------------------------------------------------------
# Test 5 — Trigger populates location_id from branch_id on INSERT
# ---------------------------------------------------------------------------

async def test_trigger_populates_location_id_from_branch_id(raw_conn):
    """
    INSERT into `orders` (which has branch_id) with only restaurant_id +
    branch_id set (no org_id / location_id). The BEFORE INSERT trigger
    (_auto_populate_org_location_branch) must fill BOTH org_id and location_id.

    Orders is the canonical Location-level table with branch_id.
    """
    # Find a Sucursal (branch) in the mapping so we can supply branch_id.
    row = await raw_conn.fetchrow(
        """
        SELECT m_branch.old_restaurant_id AS branch_rid,
               m_branch.org_id,
               m_branch.location_id,
               m_parent.old_restaurant_id AS parent_rid
        FROM _migration_restaurant_to_location m_branch
        JOIN _migration_restaurant_to_location m_parent
            ON m_parent.org_id = m_branch.org_id
           AND m_parent.old_restaurant_id <> m_branch.old_restaurant_id
        LIMIT 1
        """
    )
    if not row:
        # Fall back to using a Matriz as both restaurant and branch.
        row = await raw_conn.fetchrow(
            "SELECT old_restaurant_id AS branch_rid, org_id, location_id, "
            "       old_restaurant_id AS parent_rid "
            "FROM _migration_restaurant_to_location LIMIT 1"
        )
    if not row:
        pytest.skip("No mapping rows found")

    parent_rid = row["parent_rid"]
    branch_rid = row["branch_rid"]
    expected_org = row["org_id"]
    expected_loc = row["location_id"]

    await _set_org_id(raw_conn, expected_org)
    await _set_restaurant_id(raw_conn, parent_rid)

    # Minimal INSERT into orders — only required fields + restaurant/branch ids.
    inserted = await raw_conn.fetchrow(
        """
        INSERT INTO orders
            (restaurant_id, branch_id, org_id, location_id,
             phone, order_type, status, total, items, created_at)
        VALUES
            ($1, $2, NULL, NULL,
             '+573009999992', 'domicilio', 'pendiente', 1000, '[]'::jsonb, NOW())
        RETURNING org_id, location_id
        """,
        parent_rid, branch_rid,
    )

    assert inserted is not None, "INSERT returned no row"
    assert inserted["org_id"] == expected_org, (
        f"Expected org_id={expected_org}, got {inserted['org_id']}"
    )
    assert inserted["location_id"] == expected_loc, (
        f"Expected location_id={expected_loc} (from branch_id={branch_rid}), "
        f"got {inserted['location_id']}"
    )


# ---------------------------------------------------------------------------
# Test 6 — Cross-tenant INSERT is still blocked (both policy paths)
# ---------------------------------------------------------------------------

async def test_cross_tenant_insert_still_blocked(raw_conn):
    """
    WITH CHECK on both policies must reject INSERTs where the written
    restaurant_id / org_id does not match the active GUC.

    Scenario A: GUC set to tenant A, INSERT with restaurant_id of tenant B
                → tenant_isolation WITH CHECK rejects it.
    Scenario B: GUC set to org A, INSERT with org_id of org B
                → org_isolation WITH CHECK rejects it.
    """
    # Get two different orgs from the mapping.
    rows = await raw_conn.fetch(
        "SELECT DISTINCT org_id FROM _migration_restaurant_to_location LIMIT 2"
    )
    if len(rows) < 2:
        pytest.skip("Need at least 2 distinct orgs for cross-tenant test")

    org_a = rows[0]["org_id"]
    org_b = rows[1]["org_id"]

    # Get corresponding restaurant_ids.
    rid_a = await raw_conn.fetchval(
        "SELECT old_restaurant_id FROM _migration_restaurant_to_location "
        "WHERE org_id = $1 LIMIT 1",
        org_a,
    )
    rid_b = await raw_conn.fetchval(
        "SELECT old_restaurant_id FROM _migration_restaurant_to_location "
        "WHERE org_id = $1 LIMIT 1",
        org_b,
    )

    # Scenario A: GUC = org_a / rid_a, write restaurant_id = rid_b
    await _set_restaurant_id(raw_conn, rid_a)
    await _set_org_id(raw_conn, org_a)

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await raw_conn.execute(
            """
            INSERT INTO conversations
                (phone, bot_number, restaurant_id, org_id, history, created_at)
            VALUES
                ('+573001111111', '+571111111111', $1, $2, '[]'::jsonb, NOW())
            """,
            rid_b, org_b,  # wrong tenant
        )

    # Scenario B: GUC = org_b, write org_id = org_a (also wrong restaurant_id).
    # The insert must fail on at least one of the two WITH CHECK clauses.
    await _set_restaurant_id(raw_conn, rid_b)
    await _set_org_id(raw_conn, org_b)

    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
        await raw_conn.execute(
            """
            INSERT INTO conversations
                (phone, bot_number, restaurant_id, org_id, history, created_at)
            VALUES
                ('+573002222222', '+571222222222', $1, $2, '[]'::jsonb, NOW())
            """,
            rid_a, org_a,  # mismatched with GUC
        )
