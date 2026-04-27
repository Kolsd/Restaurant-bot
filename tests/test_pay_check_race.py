"""
tests/test_pay_check_race.py
============================
Integration tests for the race-free pay_check flow (claim → finalize → release).

Bug fixed: TOP-1 of the audit. Two cashiers paying the same check within
seconds would both pass db_get_check + the Python status check, both call DIAN,
both UPDATE table_checks → double DIAN invoice + double loyalty + payments JSON
overwrite.

Fix: db_claim_check_for_payment uses SELECT FOR UPDATE + transition open→paying
atomically. The second cashier sees status='paying' and gets None back, so the
route returns 409 BEFORE generating any DIAN invoice.

These tests use REAL Postgres (TEST_DATABASE_URL) and TWO concurrent connections
to actually exercise the race protection. The single-connection fixture pattern
used by test_loyalty_aggregates.py would defeat the purpose.

Test matrix
-----------
  test_concurrent_claim_only_one_wins  — asyncio.gather of 2 claims → 1 wins, 1 None
  test_claim_releases_to_open          — release rolls back paying → open
  test_finalize_requires_paying_state  — finalize without prior claim returns False
  test_double_finalize_only_first_wins — finalize twice on same paying check → only 1
"""

import os
import uuid
import asyncio
import pytest
import asyncpg


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _set_scope(conn, org_id: int) -> None:
    """Set RLS GUC for current connection."""
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


async def _seed_check(pool, org_id: int, location_id: int, tag: str) -> tuple[str, str, str]:
    """Create the minimal data for a payable check.

    Returns (base_order_id, check_id, table_id).

    Commits the data so it's visible to multiple connections (race test needs that).
    Caller must clean up via _cleanup_check.
    """
    base_order_id = f"race-test-{tag}-order"
    check_id = f"race-test-{tag}-check"
    table_id = f"race-test-{tag}-table"

    async with pool.acquire() as conn:
        await conn.execute("SET LOCAL ROLE mesio_app")
        await _set_scope(conn, org_id)
        # restaurant_tables: required because table_orders.table_id is NOT NULL.
        # The branch_id column is legacy (Wave-2 left it for compat) — point it
        # at location_id which is the canonical sede.
        await conn.execute(
            """
            INSERT INTO restaurant_tables
                (id, org_id, location_id, branch_id, name, number)
            VALUES ($1, $2::int, $3::int, $3::int, 'TestTable', 999)
            """,
            table_id, org_id, location_id,
        )
        # table_orders: minimum non-null fields. items must be JSON.
        await conn.execute(
            """
            INSERT INTO table_orders
                (id, org_id, location_id, base_order_id, sub_number,
                 table_id, table_name, phone, items, status, total)
            VALUES ($1, $2::int, $3::int, $1, 1, $4, 'TestTable',
                    'manual', '[]'::jsonb, 'recibido', 50000)
            """,
            base_order_id, org_id, location_id, table_id,
        )
        # table_checks: status='open' is the starting point we're protecting.
        # NOTE: table_checks has NO org_id/location_id columns and NO RLS today
        # (security gap flagged for follow-up). Tenant scoping is implicit via
        # JOIN to table_orders.
        await conn.execute(
            """
            INSERT INTO table_checks
                (id, base_order_id, check_number,
                 items, subtotal, tax_amount, total, status)
            VALUES ($1, $2, 1, '[]'::jsonb, 50000, 0, 50000, 'open')
            """,
            check_id, base_order_id,
        )
    return base_order_id, check_id, table_id


async def _cleanup_check(pool, org_id: int, base_order_id: str, table_id: str) -> None:
    """Best-effort teardown. RLS-scoped delete."""
    try:
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await _set_scope(conn, org_id)
            await conn.execute("DELETE FROM table_checks WHERE base_order_id = $1", base_order_id)
            await conn.execute("DELETE FROM table_orders WHERE base_order_id = $1", base_order_id)
            await conn.execute("DELETE FROM restaurant_tables WHERE id = $1", table_id)
    except Exception:
        # Tests should fail loudly via assertions, not via teardown noise.
        pass


async def _get_or_create_test_location(pool) -> tuple[int, int]:
    """Return (org_id, location_id) of a stable seed restaurant for race tests.

    Uses an idempotent slug so re-runs don't accumulate junk. The org_id+location_id
    pair is committed (NOT inside a rolled-back tx) so subsequent test runs reuse it.
    """
    async with pool.acquire() as conn:
        # Bypass RLS for the lookup/create — we're operating on the orgs table itself.
        org_row = await conn.fetchrow(
            """
            INSERT INTO organizations (name, slug)
            VALUES ('PayCheckRaceTestOrg', 'paycheck-race-test-org')
            ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
            RETURNING id
            """
        )
        org_id = org_row["id"]

        # Look-up first; if missing, INSERT. Two-step keeps asyncpg's type
        # inference simple (avoids bigint = text confusion in WHERE NOT EXISTS).
        existing = await conn.fetchrow(
            "SELECT id FROM locations WHERE org_id = $1::int ORDER BY id ASC LIMIT 1",
            org_id,
        )
        if existing is not None:
            return org_id, existing["id"]

        loc_row = await conn.fetchrow(
            """
            INSERT INTO locations (org_id, name, address, whatsapp_number)
            VALUES ($1::int, 'PayCheckRaceTestLoc', 'Test Address',
                    'paycheck-race-test-' || $1::int::text)
            RETURNING id
            """,
            org_id,
        )
    return org_id, loc_row["id"]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def real_pool():
    """Real asyncpg pool with at least 2 connections for true concurrency."""
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=2, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def patched_pool(real_pool, monkeypatch):
    """Monkey-patch get_pool to return our real_pool — so tenant_connection() uses it.

    Unlike test_loyalty_aggregates which uses a single-conn shim, the race test
    NEEDS multiple connections, so we yield the real pool unwrapped.
    """
    from app.services import database as db_module

    async def _fake_get_pool():
        return real_pool
    monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)
    yield real_pool


@pytest.fixture
async def seeded_check(patched_pool):
    """Create a paid-able check, yield (org_id, location_id, base_order_id, check_id)."""
    org_id, location_id = await _get_or_create_test_location(patched_pool)
    tag = uuid.uuid4().hex[:8]
    base_order_id, check_id, table_id = await _seed_check(patched_pool, org_id, location_id, tag)
    try:
        yield org_id, location_id, base_order_id, check_id
    finally:
        await _cleanup_check(patched_pool, org_id, base_order_id, table_id)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_wins(seeded_check):
    """Two simultaneous claims on the same check: exactly one returns dict, one None.

    This is the regression test for the TOP-1 audit bug. Without SELECT FOR UPDATE
    in db_claim_check_for_payment, both calls would get the row and both would
    UPDATE — leading to double DIAN.
    """
    from app.repositories.tables_repo import db_claim_check_for_payment
    from app.services.tenant_context import tenant_scope

    org_id, _, base_order_id, check_id = seeded_check

    async def _claim():
        with tenant_scope(org_id):
            return await db_claim_check_for_payment(check_id, base_order_id)

    # Fire both claims concurrently.
    results = await asyncio.gather(_claim(), _claim())

    wins = [r for r in results if r is not None]
    losses = [r for r in results if r is None]
    assert len(wins) == 1, f"Expected exactly 1 winning claim, got {len(wins)}: {results}"
    assert len(losses) == 1, f"Expected exactly 1 losing claim, got {len(losses)}: {results}"
    # Winner sees the new status reflected.
    assert wins[0]["status"] == "paying"


@pytest.mark.asyncio
async def test_claim_releases_to_open(seeded_check):
    """db_release_check rolls back paying → open, so a retry can succeed."""
    from app.repositories.tables_repo import (
        db_claim_check_for_payment,
        db_release_check,
        db_get_check,
    )
    from app.services.tenant_context import tenant_scope

    org_id, _, base_order_id, check_id = seeded_check

    with tenant_scope(org_id):
        first = await db_claim_check_for_payment(check_id, base_order_id)
        assert first is not None, "First claim should win on a fresh check"

        released = await db_release_check(check_id)
        assert released is True, "Release should succeed on a paying check"

        # After release, status must be back to 'open'.
        check_after = await db_get_check(check_id)
        assert check_after is not None
        assert check_after["status"] == "open"

        # And a fresh claim must succeed again.
        second = await db_claim_check_for_payment(check_id, base_order_id)
        assert second is not None, "Re-claim after release should succeed"

        # Cleanup: leave the check in 'open' so teardown deletes it cleanly.
        await db_release_check(check_id)


@pytest.mark.asyncio
async def test_release_on_open_check_is_noop(seeded_check):
    """db_release_check on an 'open' check returns False (not paying)."""
    from app.repositories.tables_repo import db_release_check
    from app.services.tenant_context import tenant_scope

    org_id, _, _, check_id = seeded_check

    with tenant_scope(org_id):
        # Check is fresh — never claimed. Release should be a no-op.
        result = await db_release_check(check_id)
        assert result is False


@pytest.mark.asyncio
async def test_finalize_requires_paying_state(seeded_check):
    """db_finalize_check_payment without prior claim returns False — protects against
    bypass of the claim-finalize protocol."""
    from app.repositories.tables_repo import db_finalize_check_payment
    from app.services.tenant_context import tenant_scope

    org_id, _, base_order_id, check_id = seeded_check

    with tenant_scope(org_id):
        # No claim was made — check is still 'open'. Finalize should refuse.
        result = await db_finalize_check_payment(
            check_id=check_id,
            base_order_id=base_order_id,
            payments=[{"method": "cash", "amount": 50000}],
            change_amount=0,
            fiscal_invoice_id=None,
        )
        assert result is False, "Finalize without prior claim must return False"


@pytest.mark.asyncio
async def test_double_finalize_only_first_wins(seeded_check):
    """Even if claim is bypassed via direct UPDATE, double finalize only runs once.

    This validates the AND status='paying' guard inside db_finalize_check_payment.
    """
    from app.repositories.tables_repo import (
        db_claim_check_for_payment,
        db_finalize_check_payment,
    )
    from app.services.tenant_context import tenant_scope

    org_id, _, base_order_id, check_id = seeded_check

    with tenant_scope(org_id):
        claimed = await db_claim_check_for_payment(check_id, base_order_id)
        assert claimed is not None

        async def _finalize():
            return await db_finalize_check_payment(
                check_id=check_id,
                base_order_id=base_order_id,
                payments=[{"method": "cash", "amount": 50000}],
                change_amount=0,
                fiscal_invoice_id=None,
            )

        results = await asyncio.gather(_finalize(), _finalize())
        wins = [r for r in results if r is True]
        losses = [r for r in results if r is False]
        assert len(wins) == 1, f"Expected exactly 1 successful finalize, got {len(wins)}"
        assert len(losses) == 1, f"Expected exactly 1 noop finalize, got {len(losses)}"
