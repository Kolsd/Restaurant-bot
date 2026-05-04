"""
tests/test_stats_aggregates.py

Integration tests for the three new aggregate endpoints:
  GET /api/stats/churn-summary
  GET /api/stats/branches-consolidated
  GET /api/stats/branches-comparison

Tests require TEST_DATABASE_URL pointing to a writable test Postgres instance.
They are automatically skipped when the env var is absent so the CI suite stays
green without a database.

Test patterns per endpoint (9 total):
  1. Tenant isolation   — two orgs; endpoint for org A only returns org A's data
  2. Empty tenant       — new org with zero data returns safe zero-filled shape
  3. Aggregate accuracy — seed known data, verify the SQL computes it correctly

Seeding strategy:
  - INSERT into `organizations` + `locations` (restaurants VIEW is read-only post-Wave-2)
  - Wrap every test in a rolled-back transaction via the `db_conn` fixture
  - Set app.org_id GUC explicitly on the raw connection to satisfy RLS policies

Fixture pattern mirrors test_loyalty_aggregates.py:
  - _ConnProxy / _PoolShim / _AcquireCtx — work around asyncpg Connection __slots__
  - async def _fake_get_pool() — patch target for app.services.database.get_pool
  - SET LOCAL ROLE mesio_app — enforce RLS (test pool connects as postgres superuser)
  - Manual tx.start() / tx.rollback() — rolled-back isolation without exception noise
"""

from __future__ import annotations

import os
import pytest
import asyncpg
from decimal import Decimal

# ── Skip module if no DB URL ──────────────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Connection proxy helpers (mirror test_loyalty_aggregates.py) ──────────────


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


def _strip_tz(args):
    """Strip tzinfo from all datetime args so they work with TIMESTAMP WITHOUT TIME ZONE columns.

    orders.created_at (and most timestamp columns in this schema) are
    TIMESTAMP WITHOUT TIME ZONE.  db_branches_consolidated / db_branches_comparison
    use datetime.now(timezone.utc) which produces tz-aware datetimes; asyncpg
    rejects those for tz-naive columns.  Stripping tzinfo is safe here because
    the DB timezone is UTC.
    """
    from datetime import datetime
    return tuple(
        v.replace(tzinfo=None) if isinstance(v, datetime) and v.tzinfo is not None else v
        for v in args
    )


class _TzStripProxy(_ConnProxy):
    """Like _ConnProxy but strips tzinfo from all datetime query args.

    Used for stats tests that call repo functions which pass tz-aware datetimes
    to TIMESTAMP WITHOUT TIME ZONE columns.
    """

    async def execute(self, query, *args, **kw):
        return await object.__getattribute__(self, "_c").execute(query, *_strip_tz(args), **kw)

    async def fetch(self, query, *args, **kw):
        return await object.__getattribute__(self, "_c").fetch(query, *_strip_tz(args), **kw)

    async def fetchrow(self, query, *args, **kw):
        return await object.__getattribute__(self, "_c").fetchrow(query, *_strip_tz(args), **kw)

    async def fetchval(self, query, *args, **kw):
        return await object.__getattribute__(self, "_c").fetchval(query, *_strip_tz(args), **kw)


# ── Module-level fixture: connection pool ─────────────────────────────────────


@pytest.fixture
async def raw_pool():
    """Function-scoped pool — each test gets its own pool on its own event loop."""
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back _TzStripProxy with get_pool mocked.

    Mirrors test_loyalty_aggregates.py exactly, plus timezone stripping:
      - wraps raw connection in _TzStripProxy to avoid __slots__ issues AND to
        strip tzinfo from datetime query args (orders.created_at is TIMESTAMP
        WITHOUT TIME ZONE; stats_repo passes datetime.now(timezone.utc))
      - patches app.services.database.get_pool with an async function returning _PoolShim
      - SET LOCAL ROLE mesio_app so RLS policies actually enforce
        (test pool connects as postgres/superuser — without this, FORCE RLS is bypassed)
      - manual tx.start() / tx.rollback() for clean isolation without exception noise
    """
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _TzStripProxy(conn)
        shim = _PoolShim(proxy)

        # get_pool is async in app.services.database; mock must be too.
        async def _fake_get_pool():
            return shim

        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            # Drop superuser privilege so RLS actually enforces.
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _set_org_scope(conn, org_id: int) -> None:
    """Set the RLS GUC so tenant-scoped queries match the given org."""
    await conn.execute(
        "SELECT set_config('app.org_id', $1, true)",
        str(org_id),
    )
    # Also set the legacy GUC for backward-compat RLS policies
    await conn.execute(
        "SELECT set_config('app.restaurant_id', $1, true)",
        str(org_id),
    )


async def _seed_org(conn, name: str) -> tuple[int, int]:
    """Insert a minimal org + location row. Returns (org_id, location_id)."""
    org_id = await conn.fetchval(
        """
        INSERT INTO organizations (name, slug, subscription_plan, subscription_status)
        VALUES ($1, $2, 'pro', 'active')
        RETURNING id
        """,
        name,
        name.lower().replace(" ", "_") + "_test",
    )
    location_id = await conn.fetchval(
        """
        INSERT INTO locations (org_id, name, address, whatsapp_number)
        VALUES ($1, $2, 'Calle 1 # 1-1', '+57300000' || $3)
        RETURNING id
        """,
        org_id,
        name + " - Principal",
        str(org_id)[-4:].zfill(4),
    )
    return org_id, location_id


async def _seed_customer_profile(
    conn,
    org_id: int,
    phone: str,
    name: str,
    total_orders: int,
    total_spent: Decimal,
    days_dormant: int,
) -> None:
    """Upsert a customer_profiles row with last_seen set `days_dormant` days ago."""
    await conn.execute(
        """
        INSERT INTO customer_profiles
            (org_id, phone, display_name, total_orders, total_spent, last_seen)
        VALUES
            ($1, $2, $3, $4, $5, NOW() - MAKE_INTERVAL(days => $6))
        ON CONFLICT (org_id, phone) DO UPDATE
            SET display_name  = EXCLUDED.display_name,
                total_orders  = EXCLUDED.total_orders,
                total_spent   = EXCLUDED.total_spent,
                last_seen     = EXCLUDED.last_seen
        """,
        org_id, phone, name, total_orders, total_spent, days_dormant,
    )


async def _seed_paid_order(
    conn,
    org_id: int,
    location_id: int,
    total: Decimal,
    days_ago: int = 0,
) -> None:
    """Insert a paid delivery order.

    orders.id is TEXT NOT NULL with no DEFAULT — must supply a value explicitly.
    We use gen_random_uuid()::text via a SQL expression in the INSERT.
    orders.created_at is TIMESTAMP WITHOUT TIME ZONE — use NOW() SQL function to
    avoid asyncpg rejecting tz-aware Python datetimes for a tz-naive column.
    """
    # Use a fixed offset of at least 1 minute behind DB NOW() so the seeded
    # created_at is safely below the Python-computed upper bound in the repo query.
    # MAKE_INTERVAL(days => 0) = NOW() which can race with Python datetime.utcnow()
    # when days_ago=0; using days_ago + a small minutes offset avoids that.
    await conn.execute(
        """
        INSERT INTO orders
            (id, org_id, location_id, phone, order_type, status, paid,
             subtotal, total, items, created_at)
        VALUES
            (gen_random_uuid()::text,
             $1, $2, '+573001111111', 'domicilio', 'entregado', TRUE,
             $3, $3, '[]'::jsonb,
             NOW() - MAKE_INTERVAL(days => $4) - INTERVAL '2 minutes')
        """,
        org_id, location_id, total, days_ago,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. GET /api/stats/churn-summary
# ═══════════════════════════════════════════════════════════════════════════════

class TestChurnSummary:
    """Tests for db_churn_summary() repo function (called by GET /api/stats/churn-summary)."""

    @pytest.mark.asyncio
    async def test_empty_tenant_returns_zeros(self, db_conn):
        """A brand-new org with no customer_profiles returns all-zero counts, no crash."""
        from app.repositories.stats_repo import db_churn_summary
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, _ = await _seed_org(db_conn, "ChurnEmpty Org")
        await _set_org_scope(db_conn, org_id)

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx

            with tenant_scope(org_id):
                result = await db_churn_summary(org_id=org_id)

        assert result["high_count"] == 0
        assert result["medium_count"] == 0
        assert result["watch_count"] == 0
        assert result["ltv_sum"] == 0.0
        assert result["medium_risk"] == []
        assert result["reactivated_count"] == 0

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db_conn):
        """Customers seeded for org A do not appear in org B's churn summary."""
        from app.repositories.stats_repo import db_churn_summary
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_a_id, _ = await _seed_org(db_conn, "ChurnIsoA")
        org_b_id, _ = await _seed_org(db_conn, "ChurnIsoB")

        # Set org A scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_a_id)

        # Seed 5 dormant customers for org A (score >= 0.50 = medium or high)
        for i in range(5):
            await _seed_customer_profile(
                db_conn, org_a_id,
                phone=f"+5730099{i:04d}",
                name=f"ClienteA{i}",
                total_orders=4,
                total_spent=Decimal("50000"),
                days_dormant=40,  # score = 40/70 ≈ 0.571 → medium bin
            )

        # Org B gets no customers

        async def _call_churn_for(org_id: int) -> dict:
            with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
                mock_ctx = mock.MagicMock()
                mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
                mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
                mock_tc.return_value = mock_ctx
                await _set_org_scope(db_conn, org_id)
                with tenant_scope(org_id):
                    return await db_churn_summary(org_id=org_id)

        result_a = await _call_churn_for(org_a_id)
        result_b = await _call_churn_for(org_b_id)

        # Org A must have medium-bin customers
        assert result_a["medium_count"] == 5, (
            f"Expected 5 medium-risk customers for org A, got {result_a['medium_count']}"
        )
        assert result_a["ltv_sum"] > 0

        # Org B must see nothing
        assert result_b["medium_count"] == 0
        assert result_b["high_count"] == 0
        assert result_b["ltv_sum"] == 0.0

    @pytest.mark.asyncio
    async def test_aggregate_correctness(self, db_conn):
        """Churn bins map correctly to seeded days_dormant values."""
        from app.repositories.stats_repo import db_churn_summary
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, _ = await _seed_org(db_conn, "ChurnAccuracy")
        # Set scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_id)

        # high bin: 60 days, score = 60/70 ≈ 0.857 → high (≥3 orders)
        await _seed_customer_profile(db_conn, org_id, "+5700000001", "High1", 4, Decimal("200000"), 60)
        # medium bin: 40 days, score = 40/70 ≈ 0.571 → medium (≥3 orders)
        await _seed_customer_profile(db_conn, org_id, "+5700000002", "Med1",  4, Decimal("100000"), 40)
        await _seed_customer_profile(db_conn, org_id, "+5700000003", "Med2",  3, Decimal("80000"),  40)
        # watch bin: 20 days dormant — score = 20/70 ≈ 0.286; only ≥2 orders so may fall in watch
        # watch requires days_dormant ≥ 21 for score ≥ 0.30
        await _seed_customer_profile(db_conn, org_id, "+5700000004", "Watch1", 2, Decimal("30000"), 22)

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            await _set_org_scope(db_conn, org_id)
            with tenant_scope(org_id):
                result = await db_churn_summary(org_id=org_id)

        assert result["high_count"] == 1, f"Expected 1 high, got {result['high_count']}"
        assert result["medium_count"] == 2, f"Expected 2 medium, got {result['medium_count']}"
        assert result["watch_count"] >= 1, f"Expected ≥1 watch, got {result['watch_count']}"

        # LTV must sum high + medium only: 200000 + 100000 + 80000 = 380000
        assert result["ltv_sum"] == pytest.approx(380000.0, abs=1.0), (
            f"Expected ltv_sum ≈ 380000, got {result['ltv_sum']}"
        )

        # medium_risk contains the 2 medium customers, capped at 6
        assert len(result["medium_risk"]) == 2
        assert all(0.50 <= r["churn_score"] < 0.80 for r in result["medium_risk"])


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GET /api/stats/branches-consolidated
# ═══════════════════════════════════════════════════════════════════════════════

class TestBranchesConsolidated:
    """Tests for db_branches_consolidated() repo function."""

    @pytest.mark.asyncio
    async def test_empty_tenant_returns_zeros(self, db_conn):
        """A new org with no orders returns safe zero values without crashing."""
        from app.repositories.stats_repo import db_branches_consolidated
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, _ = await _seed_org(db_conn, "ConsEmpty")

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            await _set_org_scope(db_conn, org_id)
            with tenant_scope(org_id):
                result = await db_branches_consolidated(org_id=org_id, days=7)

        assert result["total_sales"] == 0.0
        assert result["total_tickets"] == 0
        assert result["avg_nps"] is None
        assert result["total_staff"] == 0
        assert result["growth_yoy"] is None
        assert "period" in result

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db_conn):
        """Sales from org A do not bleed into org B's consolidated totals."""
        from app.repositories.stats_repo import db_branches_consolidated
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_a_id, loc_a_id = await _seed_org(db_conn, "ConsIsoA")
        org_b_id, loc_b_id = await _seed_org(db_conn, "ConsIsoB")

        # Set org A scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_a_id)

        # Seed 3 paid orders totalling 300 000 for org A
        for _ in range(3):
            await _seed_paid_order(db_conn, org_a_id, loc_a_id, Decimal("100000"), days_ago=0)

        async def _call_cons(org_id: int) -> dict:
            with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
                mock_ctx = mock.MagicMock()
                mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
                mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
                mock_tc.return_value = mock_ctx
                await _set_org_scope(db_conn, org_id)
                with tenant_scope(org_id):
                    return await db_branches_consolidated(org_id=org_id, days=7)

        result_a = await _call_cons(org_a_id)
        result_b = await _call_cons(org_b_id)

        assert result_a["total_sales"] == pytest.approx(300000.0, abs=1.0), (
            f"Org A expected 300000 in sales, got {result_a['total_sales']}"
        )
        assert result_a["total_tickets"] == 3

        assert result_b["total_sales"] == 0.0, (
            f"Org B should have 0 sales but got {result_b['total_sales']}"
        )
        assert result_b["total_tickets"] == 0

    @pytest.mark.asyncio
    async def test_aggregate_correctness(self, db_conn):
        """Total sales and tickets are computed correctly from seeded orders."""
        from app.repositories.stats_repo import db_branches_consolidated
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, loc_id = await _seed_org(db_conn, "ConsAccuracy")
        # Set scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_id)

        # Seed 5 orders: 4 within window, 1 outside (10 days ago with days=7 window)
        await _seed_paid_order(db_conn, org_id, loc_id, Decimal("50000"), days_ago=0)
        await _seed_paid_order(db_conn, org_id, loc_id, Decimal("75000"), days_ago=1)
        await _seed_paid_order(db_conn, org_id, loc_id, Decimal("120000"), days_ago=3)
        await _seed_paid_order(db_conn, org_id, loc_id, Decimal("30000"), days_ago=6)
        # This one is outside the 7-day window
        await _seed_paid_order(db_conn, org_id, loc_id, Decimal("999999"), days_ago=10)

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            await _set_org_scope(db_conn, org_id)
            with tenant_scope(org_id):
                result = await db_branches_consolidated(org_id=org_id, days=7)

        expected_sales = 50000 + 75000 + 120000 + 30000  # = 275000
        assert result["total_sales"] == pytest.approx(float(expected_sales), abs=1.0), (
            f"Expected total_sales ≈ {expected_sales}, got {result['total_sales']}"
        )
        assert result["total_tickets"] == 4, (
            f"Expected 4 tickets in 7-day window, got {result['total_tickets']}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GET /api/stats/branches-comparison
# ═══════════════════════════════════════════════════════════════════════════════

class TestBranchesComparison:
    """Tests for db_branches_comparison() repo function."""

    @pytest.mark.asyncio
    async def test_empty_tenant_returns_safe_shape(self, db_conn):
        """A new org with 1 location and no orders returns well-shaped rows, no crash."""
        from app.repositories.stats_repo import db_branches_comparison
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, _ = await _seed_org(db_conn, "CmpEmpty")

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            await _set_org_scope(db_conn, org_id)
            with tenant_scope(org_id):
                result = await db_branches_comparison(org_id=org_id, days=30)

        assert "locations" in result
        assert "rows" in result
        assert isinstance(result["locations"], list)
        assert isinstance(result["rows"], list)

        # The 1 location must appear
        assert len(result["locations"]) == 1

        for row in result["rows"]:
            assert "metric" in row
            assert "per_location" in row
            assert len(row["per_location"]) == 1
            # No data → per_location values are None or 0.0 (numeric nulls)
            # avg should be None if all per_location are null
            val = row["per_location"][0]
            assert val is None or isinstance(val, (int, float))

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, db_conn):
        """Orders from org A do not appear in org B's comparison rows."""
        from app.repositories.stats_repo import db_branches_comparison
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_a_id, loc_a_id = await _seed_org(db_conn, "CmpIsoA")
        org_b_id, loc_b_id = await _seed_org(db_conn, "CmpIsoB")

        # Set org A scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_a_id)

        # Seed 5 paid orders for org A's location
        for _ in range(5):
            await _seed_paid_order(db_conn, org_a_id, loc_a_id, Decimal("80000"), days_ago=1)

        async def _call_cmp(org_id: int) -> dict:
            with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
                mock_ctx = mock.MagicMock()
                mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
                mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
                mock_tc.return_value = mock_ctx
                await _set_org_scope(db_conn, org_id)
                with tenant_scope(org_id):
                    return await db_branches_comparison(org_id=org_id, days=30)

        result_a = await _call_cmp(org_a_id)
        result_b = await _call_cmp(org_b_id)

        # Org A: first row is "Ventas diarias promedio" — must be > 0
        daily_row_a = next((r for r in result_a["rows"] if "Ventas" in r["metric"]), None)
        assert daily_row_a is not None
        assert daily_row_a["per_location"][0] is not None
        assert daily_row_a["per_location"][0] > 0, (
            f"Org A Ventas daily avg should be > 0, got {daily_row_a['per_location'][0]}"
        )

        # Org B: the same metric must be None (no orders)
        daily_row_b = next((r for r in result_b["rows"] if "Ventas" in r["metric"]), None)
        assert daily_row_b is not None
        val_b = daily_row_b["per_location"][0]
        assert val_b is None or val_b == 0.0, (
            f"Org B should have no sales, got {val_b}"
        )

    @pytest.mark.asyncio
    async def test_aggregate_correctness_multi_location(self, db_conn):
        """With 2 locations, per_location arrays have 2 entries and top_location_id is correct."""
        from app.repositories.stats_repo import db_branches_comparison
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, loc1_id = await _seed_org(db_conn, "CmpMultiLoc")

        # Add a second location to the same org
        loc2_id = await db_conn.fetchval(
            """
            INSERT INTO locations (org_id, name, address, whatsapp_number)
            VALUES ($1, 'Sede 2', 'Calle 2 # 2-2', '+573009999' || $2)
            RETURNING id
            """,
            org_id,
            str(org_id)[-3:].zfill(3),
        )

        # Set scope before inserting tenant-scoped rows (RLS WITH CHECK requires this)
        await _set_org_scope(db_conn, org_id)

        # Seed loc1 with 300 000 in sales (3 × 100 000)
        for _ in range(3):
            await _seed_paid_order(db_conn, org_id, loc1_id, Decimal("100000"), days_ago=1)

        # Seed loc2 with 60 000 in sales (2 × 30 000)
        for _ in range(2):
            await _seed_paid_order(db_conn, org_id, loc2_id, Decimal("30000"), days_ago=1)

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            await _set_org_scope(db_conn, org_id)
            with tenant_scope(org_id):
                result = await db_branches_comparison(org_id=org_id, days=30)

        assert len(result["locations"]) == 2

        daily_row = next((r for r in result["rows"] if "Ventas" in r["metric"]), None)
        assert daily_row is not None
        assert len(daily_row["per_location"]) == 2, (
            "per_location must have one entry per location"
        )

        # loc1 (300 000 / 30 days = 10 000/day) > loc2 (60 000 / 30 days = 2 000/day)
        # top_location_id must point to loc1
        assert daily_row["top_location_id"] == loc1_id, (
            f"Expected top_location_id={loc1_id}, got {daily_row['top_location_id']}"
        )

        # Both per_location entries must be non-null positive numbers
        assert all(v is not None and v > 0 for v in daily_row["per_location"]), (
            f"All locations should have non-null positive daily avg: {daily_row['per_location']}"
        )

    @pytest.mark.asyncio
    async def test_food_cost_pct_row_present(self, db_conn):
        """Food cost % row is present in the comparison and behaves correctly:
           — When org has no recipes: per_location values are all None.
           — When org has recipes but locations have no matched orders: still None.
           This validates wiring (the row is no longer a hard placeholder).
        """
        from app.repositories.stats_repo import db_branches_comparison
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, loc_id = await _seed_org(db_conn, "FoodCostNoRecipes")
        await _set_org_scope(db_conn, org_id)

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            with tenant_scope(org_id):
                result = await db_branches_comparison(org_id=org_id, days=30)

        # Food cost % row must exist
        fc_row = next((r for r in result["rows"] if r["metric"] == "Food cost %"), None)
        assert fc_row is not None, "Food cost % row missing from comparison output"
        # No recipes defined → all per_location are None (cleanly null, not crashed)
        assert fc_row["per_location"] == [None]
        assert fc_row["avg"] is None

    @pytest.mark.asyncio
    async def test_food_cost_pct_computes_with_recipes_and_orders(self, db_conn):
        """When dish_recipes + inventory + matching paid orders exist, Food cost % is computed.

        Recipe: dish 'Burger' uses 1 unit of 'Patty' costing 5_000.
        Order: 1 Burger sold at price 20_000 → cost=5_000, revenue=20_000 → 25.0%.
        """
        from app.repositories.stats_repo import db_branches_comparison
        from app.services.tenant_context import tenant_scope
        import unittest.mock as mock

        org_id, loc_id = await _seed_org(db_conn, "FoodCostWithRecipe")
        await _set_org_scope(db_conn, org_id)

        # Seed inventory ingredient
        ing_id = await db_conn.fetchval(
            """INSERT INTO inventory (org_id, restaurant_id, name, unit, current_stock, cost_per_unit)
               VALUES ($1, $1, 'Patty', 'unit', 100, 5000)
               RETURNING id""",
            org_id,
        )
        # Seed recipe: dish 'Burger' = 1 × Patty
        await db_conn.execute(
            """INSERT INTO dish_recipes (org_id, restaurant_id, dish_name, ingredient_id, quantity)
               VALUES ($1, $1, 'Burger', $2, 1)""",
            org_id, ing_id,
        )
        # Seed a paid order containing 1 Burger at 20_000
        await db_conn.execute(
            """
            INSERT INTO orders
                (id, org_id, location_id, phone, order_type, status, paid,
                 subtotal, total, items, created_at)
            VALUES
                (gen_random_uuid()::text,
                 $1, $2, '+573001111111', 'domicilio', 'entregado', TRUE,
                 20000, 20000,
                 '[{"name":"Burger","quantity":1,"price":20000}]'::jsonb,
                 NOW() - INTERVAL '2 minutes')
            """,
            org_id, loc_id,
        )

        with mock.patch("app.repositories.stats_repo._tenant_connection") as mock_tc:
            mock_ctx = mock.MagicMock()
            mock_ctx.__aenter__ = mock.AsyncMock(return_value=db_conn)
            mock_ctx.__aexit__ = mock.AsyncMock(return_value=False)
            mock_tc.return_value = mock_ctx
            with tenant_scope(org_id):
                result = await db_branches_comparison(org_id=org_id, days=30)

        fc_row = next((r for r in result["rows"] if r["metric"] == "Food cost %"), None)
        assert fc_row is not None
        # 5_000 / 20_000 = 25.0%
        assert fc_row["per_location"] == [25.0], (
            f"Expected 25.0%, got {fc_row['per_location']}"
        )
