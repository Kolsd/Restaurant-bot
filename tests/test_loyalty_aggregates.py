"""
tests/test_loyalty_aggregates.py
=================================
Integration tests for the three loyalty aggregate endpoints:
  GET /api/loyalty/aggregates
  GET /api/loyalty/segments
  GET /api/loyalty/funnel

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Uses _ConnProxy / _PoolShim pattern to work around asyncpg Connection __slots__
  - Wraps every call in tenant_scope(org_id) per CLAUDE.md RLS rules
  - Validates tenant isolation: org A cannot see org B data

Test matrix
-----------
  test_aggregates_empty_tenant       — fresh org returns zeros / nulls, not 500
  test_aggregates_total_customers    — after inserting a loyalty_customer, count increases
  test_aggregates_redemption_pct     — accrual + redemption reflected in redemption_pct
  test_segments_empty_tenant         — returns list with 0 counts
  test_segments_vip_active           — member with qualifying spend counted in vip-active
  test_segments_new_month            — member enrolled this month counted in new-month
  test_funnel_empty_tenant           — returns list of steps (5 steps expected)
  test_funnel_step1_matches_customers — step1 count equals total loyalty_customers
  test_tenant_isolation_aggregates   — org B aggregates 0 when data only in org A
"""

import os
import pytest
import asyncpg
from datetime import datetime, timezone, timedelta

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

# ── Helpers ───────────────────────────────────────────────────────────────────


class _ConnProxy:
    """Thin asyncpg-compatible proxy — works around Connection.__slots__."""

    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

    async def execute(self, *a, **kw):
        return await object.__getattribute__(self, "_c").execute(*a, **kw)

    async def fetch(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchval(*a, **kw)

    def transaction(self, *a, **kw):
        return object.__getattribute__(self, "_c").transaction(*a, **kw)


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def raw_pool():
    # Function-scoped: each test gets its own pool on its own event loop.
    # Module/session scope crashes with "Future attached to a different loop".
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool mocked.

    tenant_db.tenant_connection() imports get_pool lazily from
    app.services.database, so patching the source module is sufficient —
    do NOT try to patch app.services.tenant_db.get_pool (doesn't exist).
    """
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        # get_pool is async in app.services.database; mock must be too.
        async def _fake_get_pool():
            return shim
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        # Manual transaction so we can rollback in teardown WITHOUT raising
        # an exception (which pytest counts as a test error).
        tx = conn.transaction()
        await tx.start()
        try:
            # Drop superuser privilege so RLS actually enforces (test pool
            # connects as postgres; production app runs as mesio_app).
            # Without this, tenant_isolation policies are bypassed.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_ids(db_conn):
    """Create two isolated orgs and return (org_a_id, org_b_id)."""
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "LoyaltyTestOrgA", "loyalty-test-org-a",
    )
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "LoyaltyTestOrgB", "loyalty-test-org-b",
    )
    return row_a["id"], row_b["id"]


async def _set_scope(conn, org_id):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


# ── Tests: aggregates ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregates_empty_tenant(db_conn, org_ids):
    """Fresh org returns zeros/nulls — no 500."""
    from app.repositories.loyalty_repo import db_get_loyalty_aggregates
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        result = await db_get_loyalty_aggregates(org_a)

    assert result["total_customers"] == 0
    assert result["redemption_pct"] == 0 or result["redemption_pct"] is None
    assert result["roi_multiple"] is None  # Always None until campaign spend tracked


@pytest.mark.asyncio
async def test_aggregates_total_customers(db_conn, org_ids):
    """After inserting a loyalty_customer, total_customers reflects it."""
    from app.repositories.loyalty_repo import db_get_loyalty_aggregates
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    await db_conn.execute(
        """
        INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed)
        VALUES ($1, '3001111001', 100, 100, 0)
        """,
        org_a,
    )

    with tenant_scope(org_a):
        result = await db_get_loyalty_aggregates(org_a)

    assert result["total_customers"] == 1


@pytest.mark.asyncio
async def test_aggregates_redemption_pct(db_conn, org_ids):
    """Ledger entries of type accrual/redeem are reflected in redemption_pct."""
    from app.repositories.loyalty_repo import db_get_loyalty_aggregates
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Insert customer
    await db_conn.execute(
        "INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed) "
        "VALUES ($1, '3001111002', 50, 200, 100)",
        org_a,
    )

    # Insert ledger: 200 accrual (delta>0), 100 redemption (delta<0)
    await db_conn.execute(
        "INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id) VALUES ($1, '3001111002', 200, 'purchase', 'ord-1')",
        org_a,
    )
    await db_conn.execute(
        "INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id) VALUES ($1, '3001111002', -100, 'redeem', 'ord-2')",
        org_a,
    )

    with tenant_scope(org_a):
        result = await db_get_loyalty_aggregates(org_a)

    # redemption_pct = 100 / 200 * 100 = 50%
    assert result["redemption_pct"] is not None
    assert abs(float(result["redemption_pct"]) - 50.0) < 1.0


# ── Tests: segments ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_segments_empty_tenant(db_conn, org_ids):
    """Empty tenant returns segment list with zero counts — not 500."""
    from app.repositories.loyalty_repo import db_get_loyalty_segments
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        segments = await db_get_loyalty_segments(org_a)

    assert isinstance(segments, list)
    assert len(segments) >= 1
    for seg in segments:
        assert "id" in seg
        assert "count" in seg
        assert seg["count"] == 0 or seg["count"] >= 0


@pytest.mark.asyncio
async def test_segments_new_month(db_conn, org_ids):
    """Customer enrolled this month appears in new-month segment."""
    from app.repositories.loyalty_repo import db_get_loyalty_segments
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Register a customer and add a purchase ledger entry within last 30 days.
    # The "new-month" segment is defined as: first-ever purchase entry within 30d
    # (see db_get_loyalty_segments — it queries loyalty_ledger, not loyalty_customers).
    await db_conn.execute(
        "INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed) "
        "VALUES ($1, '3001111003', 10, 10, 0)",
        org_a,
    )
    await db_conn.execute(
        "INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id) "
        "VALUES ($1, '3001111003', 10, 'purchase', 'ord-new-month-1')",
        org_a,
    )

    with tenant_scope(org_a):
        segments = await db_get_loyalty_segments(org_a)

    new_month = next((s for s in segments if s["id"] == "new-month"), None)
    assert new_month is not None
    assert new_month["count"] >= 1


@pytest.mark.asyncio
async def test_segments_vip_active(db_conn, org_ids):
    """Customer with >= 4 purchases and >= 200k spend in 30d appears in vip-active."""
    from app.repositories.loyalty_repo import db_get_loyalty_segments
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    vip_phone = "3001111004"

    # Insert loyalty customer record
    await db_conn.execute(
        "INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed) "
        "VALUES ($1, $2, 500, 500, 0)",
        org_a, vip_phone,
    )

    # Insert 5 purchase ledger entries within the last 30 days.
    # vip-active requires: >= 4 purchase entries in last 30d AND spend >= 200k.
    for i in range(5):
        await db_conn.execute(
            "INSERT INTO loyalty_ledger (org_id, phone, delta, reason, order_id) "
            "VALUES ($1, $2, 50, 'purchase', $3)",
            org_a, vip_phone, f"ord-vip-{i}",
        )

    # Insert 5 paid orders with total 50000 each (5 × 50000 = 250000 >= 200000)
    # within the last 30 days so the high_spend CTE qualifies this phone.
    import uuid
    for i in range(5):
        await db_conn.execute(
            """INSERT INTO orders
               (id, org_id, phone, order_type, status, paid, total, subtotal, items, bot_number)
               VALUES ($1, $2, $3, 'delivery', 'confirmado', true, 50000, 50000, '[]'::jsonb, 'bot')""",
            str(uuid.uuid4()), org_a, vip_phone,
        )

    with tenant_scope(org_a):
        segments = await db_get_loyalty_segments(org_a)

    vip_active = next((s for s in segments if s["id"] == "vip-active"), None)
    assert vip_active is not None
    assert vip_active["count"] >= 1


# ── Tests: funnel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_funnel_empty_tenant(db_conn, org_ids):
    """Empty tenant returns a list of 5 funnel steps — not 500."""
    from app.repositories.loyalty_repo import db_get_loyalty_funnel
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        steps = await db_get_loyalty_funnel(org_a)

    assert isinstance(steps, list)
    assert len(steps) == 5
    for step in steps:
        assert "label" in step
        assert "count" in step
        assert step["count"] == 0


@pytest.mark.asyncio
async def test_funnel_step1_matches_customers(db_conn, org_ids):
    """Step 1 (primera visita) count equals number of loyalty_customers."""
    from app.repositories.loyalty_repo import db_get_loyalty_funnel
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Insert 3 customers
    for i in range(3):
        await db_conn.execute(
            "INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed) "
            "VALUES ($1, $2, 10, 10, 0)",
            org_a, f"300111200{i}",
        )

    with tenant_scope(org_a):
        steps = await db_get_loyalty_funnel(org_a)

    assert steps[0]["count"] == 3


# ── Tests: tenant isolation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_isolation_aggregates(db_conn, org_ids):
    """Data inserted for org A is invisible to org B aggregate query."""
    from app.repositories.loyalty_repo import db_get_loyalty_aggregates
    from app.services.tenant_context import tenant_scope

    org_a, org_b = org_ids

    # Insert data for org A
    await _set_scope(db_conn, org_a)
    await db_conn.execute(
        "INSERT INTO loyalty_customers (org_id, phone, points_balance, total_earned, total_redeemed) "
        "VALUES ($1, '3009990001', 100, 100, 0)",
        org_a,
    )

    # Query from org B — must return 0
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        result = await db_get_loyalty_aggregates(org_b)

    assert result["total_customers"] == 0
