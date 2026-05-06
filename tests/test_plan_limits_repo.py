"""
tests/test_plan_limits_repo.py
================================
Integration tests for plan_limits_repo.

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Migration 0050_plan_limits must be applied to the test DB
  - Uses _ConnProxy / _PoolShim pattern (asyncpg Connection __slots__ workaround)
  - All tenant-scoped calls wrapped in tenant_scope(org_id)
  - Validates: seed data, atomicity, cap thresholds, FIFO pack consumption,
    expired pack skipping, period reset, tenant isolation, validation guards

Test matrix
-----------
  test_list_plans_seeded              — all 4 plans present with correct prices
  test_list_addons_seeded             — all 7 addons present
  test_get_plan_returns_correct       — db_get_plan returns right plan
  test_get_plan_not_found             — db_get_plan returns None for unknown
  test_increment_conv_usage_atomic    — 10 concurrent increments → final count = 10
  test_check_caps_thresholds          — 50/80/90/100% thresholds return correct statuses
  test_check_caps_comp_overrides      — comp_until active → all status = 'comp'
  test_consume_pack_credit_fifo       — FIFO consumption across multiple packs
  test_consume_pack_credit_skips_expired — expired packs not consumed
  test_reset_period                   — counters zero after reset
  test_tenant_isolation               — org A's packs invisible to org B
  test_set_auto_recharge_rejects_over_5 — max_packs > 5 raises ValueError
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Proxy/Shim helpers (reused from test_loyalty_campaigns pattern) ───────────


class _ConnProxy:
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
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=5)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        async def _fake_get_pool():
            return shim

        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_ids(db_conn):
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PlanTestOrgA", "plan-test-org-a",
    )
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "PlanTestOrgB", "plan-test-org-b",
    )
    return row_a["id"], row_b["id"]


async def _set_scope(conn, org_id):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


# ── Seed data tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_plans_seeded(db_conn):
    """db_list_plans returns all 4 canonical plans with correct monthly prices."""
    from app.repositories.plan_limits_repo import db_list_plans

    plans = await db_list_plans()
    assert len(plans) >= 4, f"Expected at least 4 plans, got {len(plans)}"

    plan_map = {p["plan_code"]: p for p in plans}
    assert "pulso" in plan_map
    assert "restaurante" in plan_map
    assert "pro" in plan_map
    assert "cadena" in plan_map

    assert plan_map["pulso"]["monthly_price_cop"] == 149_000
    assert plan_map["restaurante"]["monthly_price_cop"] == 299_000
    assert plan_map["pro"]["monthly_price_cop"] == 549_000
    assert plan_map["cadena"]["monthly_price_cop"] == 899_000


@pytest.mark.asyncio
async def test_list_addons_seeded(db_conn):
    """db_list_addons returns all 7 addon modules."""
    from app.repositories.plan_limits_repo import db_list_addons

    addons = await db_list_addons()
    assert len(addons) >= 7, f"Expected at least 7 addons, got {len(addons)}"

    module_codes = {a["module_code"] for a in addons}
    expected = {
        "extra_location", "dian", "payroll", "loyalty",
        "inventory", "reservations_deposit", "marketing_extra",
    }
    assert expected.issubset(module_codes), f"Missing addons: {expected - module_codes}"


@pytest.mark.asyncio
async def test_get_plan_returns_correct(db_conn):
    """db_get_plan returns a plan dict with correct attributes."""
    from app.repositories.plan_limits_repo import db_get_plan

    plan = await db_get_plan("restaurante")
    assert plan is not None
    assert plan["plan_code"] == "restaurante"
    assert plan["conv_cap"] == 700
    assert plan["staff_cap"] == 10
    assert plan["monthly_price_cop"] == 299_000


@pytest.mark.asyncio
async def test_get_plan_not_found(db_conn):
    """db_get_plan returns None for an unknown plan_code."""
    from app.repositories.plan_limits_repo import db_get_plan

    result = await db_get_plan("nonexistent_plan_xyz")
    assert result is None


# ── Atomicity test ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_increment_conv_usage_atomic(raw_pool, monkeypatch):
    """10 concurrent increments of conv usage produce a final count of 10.

    Uses a fresh connection per concurrent call (not the shared transaction
    fixture) so we can commit partial updates and test true atomicity.
    Wrapped in its own transaction that rolls back at the end.
    """
    from app.services import database as db_module
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_increment_conv_usage

    # We need a real pool where each coroutine gets its own connection
    # but we also need to roll back test data. Strategy: create the org in a
    # real INSERT (no transaction), run the test, then clean up.
    async with raw_pool.acquire() as setup_conn:
        await setup_conn.execute("SET LOCAL ROLE mesio_app")
        # Bypass RLS for setup using superadmin approach — insert into organizations
        # We must set org_id GUC to match WITH CHECK constraint
        row = await setup_conn.fetchrow(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            "AtomicTestOrg", "atomic-test-org-conc",
        )
        org_id = row["id"]

    # Create a shim that returns a fresh connection each time
    async def _fake_get_pool():
        class _MultiConnPoolShim:
            def acquire(self):
                return _FreshConnCtx()

            async def close(self):
                pass

        class _FreshConnCtx:
            async def __aenter__(self):
                self._conn = await raw_pool.acquire()
                return _ConnProxy(self._conn)

            async def __aexit__(self, *_):
                await raw_pool.release(self._conn)

        return _MultiConnPoolShim()

    monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

    # Set the GUC on a separate connection so the increment calls are scoped
    # We call 10 concurrent increments
    async def _one_increment():
        with tenant_scope(org_id):
            return await db_increment_conv_usage(org_id, 1)

    results = await asyncio.gather(*[_one_increment() for _ in range(10)])

    # The FINAL value (whatever result has the max) must be 10
    assert max(results) == 10, f"Expected final count 10, got max={max(results)}"

    # Cleanup — remove test org (non-scoped)
    async with raw_pool.acquire() as cleanup_conn:
        await cleanup_conn.execute(
            "DELETE FROM organizations WHERE id = $1", org_id
        )


# ── Cap status threshold tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_caps_thresholds(db_conn, org_ids):
    """db_check_caps returns correct status at 50/80/90/100% thresholds."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_check_caps

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Set org to 'pulso' plan: conv_cap=250
    await db_conn.execute(
        "UPDATE organizations SET plan_code = 'pulso' WHERE id = $1", org_a
    )

    with tenant_scope(org_a):
        # 0 used → ok
        await db_conn.execute(
            "UPDATE organizations SET current_period_convs_used = 0 WHERE id = $1", org_a
        )
        caps = await db_check_caps(org_a)
        assert caps["conv"]["status"] == "ok", f"Expected ok at 0%, got {caps['conv']['status']}"

        # 125/250 = 50% → warn50
        await db_conn.execute(
            "UPDATE organizations SET current_period_convs_used = 125 WHERE id = $1", org_a
        )
        caps = await db_check_caps(org_a)
        assert caps["conv"]["status"] == "warn50", f"Expected warn50 at 50%, got {caps['conv']['status']}"

        # 200/250 = 80% → warn80
        await db_conn.execute(
            "UPDATE organizations SET current_period_convs_used = 200 WHERE id = $1", org_a
        )
        caps = await db_check_caps(org_a)
        assert caps["conv"]["status"] == "warn80", f"Expected warn80 at 80%, got {caps['conv']['status']}"

        # 225/250 = 90% → warn90
        await db_conn.execute(
            "UPDATE organizations SET current_period_convs_used = 225 WHERE id = $1", org_a
        )
        caps = await db_check_caps(org_a)
        assert caps["conv"]["status"] == "warn90", f"Expected warn90 at 90%, got {caps['conv']['status']}"

        # 250/250 = 100% → exceeded
        await db_conn.execute(
            "UPDATE organizations SET current_period_convs_used = 250 WHERE id = $1", org_a
        )
        caps = await db_check_caps(org_a)
        assert caps["conv"]["status"] == "exceeded", f"Expected exceeded at 100%, got {caps['conv']['status']}"
        assert caps["conv"]["used"] == 250
        assert caps["conv"]["cap"] == 250


@pytest.mark.asyncio
async def test_check_caps_comp_overrides(db_conn, org_ids):
    """When comp_until is in the future, all dimension statuses return 'comp'."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_check_caps

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Set org to 'pulso' plan (low cap) and spike usage to exceeded level
    await db_conn.execute(
        "UPDATE organizations SET plan_code = 'pulso', current_period_convs_used = 300 WHERE id = $1",
        org_a,
    )
    # Set comp_until to 1 hour from now
    future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    await db_conn.execute(
        "UPDATE organizations SET comp_until = $2 WHERE id = $1",
        org_a, future,
    )

    with tenant_scope(org_a):
        caps = await db_check_caps(org_a)

    assert caps["comp_active"] is True
    assert caps["conv"]["status"] == "comp"
    assert caps["audio"]["status"] == "comp"


# ── Pack consumption tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_pack_credit_fifo(db_conn, org_ids):
    """db_consume_pack_credit drains FIFO across multiple packs."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_create_pack, db_consume_pack_credit

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        # Create two packs of 10 credits each
        await db_create_pack(org_id=org_a, credits=10, amount_paid_cop=5_000)
        await db_create_pack(org_id=org_a, credits=10, amount_paid_cop=5_000)

        # Consume 15 → should drain first pack (10) + 5 from second
        consumed = await db_consume_pack_credit(org_a, count=15)
        assert consumed == 15, f"Expected 15 consumed, got {consumed}"

        # Remaining across packs should be 5
        remaining = await db_conn.fetchval(
            "SELECT COALESCE(SUM(credits_remaining), 0) FROM usage_packs WHERE org_id = $1 AND credits_remaining > 0",
            org_a,
        )
        assert int(remaining) == 5, f"Expected 5 remaining, got {remaining}"


@pytest.mark.asyncio
async def test_consume_pack_credit_skips_expired(db_conn, org_ids):
    """db_consume_pack_credit skips packs whose expires_at has passed."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_consume_pack_credit

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Manually insert an expired pack
    past = datetime.now(tz=timezone.utc) - timedelta(days=1)
    await db_conn.execute(
        """
        INSERT INTO usage_packs
            (org_id, credits_total, credits_remaining, amount_paid_cop, expires_at)
        VALUES ($1, 50, 50, 25000, $2)
        """,
        org_a, past,
    )

    with tenant_scope(org_a):
        # Try to consume from the expired pack — should return 0 consumed
        consumed = await db_consume_pack_credit(org_a, count=10)

    assert consumed == 0, f"Expected 0 consumed (expired pack), got {consumed}"


# ── Period reset test ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_period(db_conn, org_ids):
    """db_reset_period zeroes period counters and sets current_period_start to today."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_reset_period

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Spike some usage
    await db_conn.execute(
        "UPDATE organizations SET current_period_convs_used = 99, current_period_audio_min_used = 42.5 WHERE id = $1",
        org_a,
    )

    with tenant_scope(org_a):
        await db_reset_period(org_a)

    row = await db_conn.fetchrow(
        "SELECT current_period_convs_used, current_period_audio_min_used FROM organizations WHERE id = $1",
        org_a,
    )
    assert row["current_period_convs_used"] == 0
    assert float(row["current_period_audio_min_used"]) == 0.0


# ── Tenant isolation test ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tenant_isolation(db_conn, org_ids):
    """usage_packs created for org A are invisible to org B via RLS."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_create_pack

    org_a, org_b = org_ids

    # Create pack for org A
    await _set_scope(db_conn, org_a)
    with tenant_scope(org_a):
        await db_create_pack(org_id=org_a, credits=100, amount_paid_cop=50_000)

    # Query from org B — should see 0 packs
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        count = await db_conn.fetchval(
            "SELECT COUNT(*) FROM usage_packs WHERE org_id = $1",
            org_a,  # deliberately try to read org_a's data from org_b scope
        )

    # RLS should filter it out — count must be 0
    assert int(count) == 0, f"RLS breach: org B can see {count} packs belonging to org A"


# ── Validation guard test ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_auto_recharge_rejects_over_5(db_conn, org_ids):
    """db_set_auto_recharge raises ValueError when max_packs > 5."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_set_auto_recharge

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        with pytest.raises(ValueError, match="max_packs cannot exceed 5"):
            await db_set_auto_recharge(org_id=org_a, enabled=True, max_packs=6)
