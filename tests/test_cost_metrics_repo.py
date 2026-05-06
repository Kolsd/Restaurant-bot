"""
tests/test_cost_metrics_repo.py
================================
Integration tests for cost_metrics_repo and cost_estimator.

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Uses _ConnProxy / _PoolShim pattern from test_loyalty_aggregates.py
  - All cross-tenant reads use bypass_tenant_scope per CLAUDE.md

Test matrix:
  test_empty_period_returns_zeros        — no rows → zeros, not 500
  test_cost_computation_one_million_tokens — 1M tokens → $0.60 USD by default
  test_per_restaurant_ranking            — 3 orgs seeded in desc order, assert ranking
  test_outlier_detection                 — 1 org at 5x plan limit → appears in outliers
  test_cross_tenant_visibility           — bypass scope sees all orgs
"""

from __future__ import annotations

import os
import pytest
import asyncpg
from datetime import date, timedelta
from decimal import Decimal

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Proxy helpers (from test_loyalty_aggregates.py) ───────────────────────────

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
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Rolled-back connection with get_pool mocked + RLS enforced as mesio_app."""
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
async def orgs(db_conn):
    """Create three test orgs with different subscription plans. Returns list of ids."""
    rows = []
    for name, slug, plan in [
        ("CostTestOrgFree",       "cost-test-org-free",   "free"),
        ("CostTestOrgBasic",      "cost-test-org-basic",  "basic"),
        ("CostTestOrgPro",        "cost-test-org-pro",    "pro"),
    ]:
        row = await db_conn.fetchrow(
            """
            INSERT INTO organizations (name, slug, subscription_plan)
            VALUES ($1, $2, $3) RETURNING id
            """,
            name, slug, plan,
        )
        rows.append(row["id"])
    return rows  # [free_id, basic_id, pro_id]


async def _set_org_scope(conn, org_id: int):
    """Set both GUCs so RLS is satisfied on subscription_usage inserts."""
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )
    # Legacy GUC also required by WITH CHECK policy on some environments
    await conn.execute(
        "SELECT set_config('app.restaurant_id', $1::text, true)",
        str(org_id),
    )


async def _seed_tokens(conn, org_id: int, usage_date: date, tokens: int):
    """Insert (or upsert) a subscription_usage row for testing."""
    await conn.execute(
        """
        INSERT INTO subscription_usage (org_id, usage_date, total_tokens, orders_count)
        VALUES ($1, $2, $3, 0)
        ON CONFLICT (org_id, usage_date) DO UPDATE
            SET total_tokens = EXCLUDED.total_tokens
        """,
        org_id, usage_date, tokens,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_period_returns_zeros(db_conn):
    """Empty date range → zeros / empty lists, NOT a 500."""
    from app.repositories.cost_metrics_repo import db_platform_cost_summary
    from app.services.tenant_context import bypass_tenant_scope

    today = date.today()
    # Use a future date range guaranteed to have no rows.
    future = today + timedelta(days=3650)

    with bypass_tenant_scope("internal_cost_dashboard"):
        result = await db_platform_cost_summary(future, future)

    assert result["total_tokens"] == 0
    assert result["estimated_cost_usd"] == 0.0
    assert result["estimated_cost_cop"] == 0.0
    assert result["by_day"] == []


@pytest.mark.asyncio
async def test_cost_computation_one_million_tokens():
    """1 M tokens → $0.60 USD at default rate (pure unit test, no DB)."""
    from app.services.cost_estimator import estimate_cost_usd, estimate_cost_cop, USD_TO_COP_RATE

    usd = estimate_cost_usd(1_000_000)
    assert usd == Decimal("0.600000"), f"Expected $0.60 USD, got {usd}"

    cop = estimate_cost_cop(1_000_000)
    expected_cop = (Decimal("0.600000") * USD_TO_COP_RATE).quantize(Decimal("1"))
    assert cop == expected_cop, f"Expected {expected_cop} COP, got {cop}"

    # Zero tokens → zero cost
    assert estimate_cost_usd(0) == Decimal("0.000000")
    assert estimate_cost_usd(-1) == Decimal("0.000000")


@pytest.mark.asyncio
async def test_per_restaurant_ranking(db_conn, orgs):
    """3 orgs with descending token counts → ranking matches expected order."""
    from app.repositories.cost_metrics_repo import db_per_restaurant_costs
    from app.services.tenant_context import bypass_tenant_scope

    free_id, basic_id, pro_id = orgs
    today = date.today()

    # Seed: pro=90k, basic=50k, free=10k tokens today
    for org_id, tokens in [(pro_id, 90_000), (basic_id, 50_000), (free_id, 10_000)]:
        await _set_org_scope(db_conn, org_id)
        await _seed_tokens(db_conn, org_id, today, tokens)

    with bypass_tenant_scope("internal_cost_dashboard"):
        rows = await db_per_restaurant_costs(today, today, limit=50)

    # Filter to our test orgs only (other orgs may exist in shared DB)
    test_rows = [r for r in rows if r["org_id"] in orgs]

    # Must be present and in descending order
    assert len(test_rows) == 3, f"Expected 3 test orgs, got {len(test_rows)}: {test_rows}"

    ids_in_order = [r["org_id"] for r in test_rows]
    assert ids_in_order[0] == pro_id,   "Pro org should rank highest (90k tokens)"
    assert ids_in_order[1] == basic_id, "Basic org should rank second (50k tokens)"
    assert ids_in_order[2] == free_id,  "Free org should rank last (10k tokens)"

    # Verify cost computation is non-zero for the top org
    assert test_rows[0]["estimated_cost_usd"] > 0
    assert test_rows[0]["estimated_cost_cop"] > 0


@pytest.mark.asyncio
async def test_outlier_detection(db_conn, orgs):
    """1 org at 5x its plan daily limit → appears in outliers list."""
    from app.repositories.cost_metrics_repo import db_cost_outliers
    from app.services.tenant_context import bypass_tenant_scope

    free_id, basic_id, pro_id = orgs
    today = date.today()

    # Free plan limit = 5,000 tokens/day. Seed 25,000 → 500% of limit.
    await _set_org_scope(db_conn, free_id)
    await _seed_tokens(db_conn, free_id, today, 25_000)

    # Basic plan limit = 50,000 tokens/day. Seed 10,000 → 20% → NOT an outlier.
    await _set_org_scope(db_conn, basic_id)
    await _seed_tokens(db_conn, basic_id, today, 10_000)

    with bypass_tenant_scope("internal_cost_dashboard"):
        outliers = await db_cost_outliers(today, today, threshold_pct=200)

    outlier_ids = [o["org_id"] for o in outliers]
    assert free_id in outlier_ids, (
        f"Free org at 500% of limit should be an outlier; got ids={outlier_ids}"
    )
    assert basic_id not in outlier_ids, (
        f"Basic org at 20% of limit should NOT be an outlier; got ids={outlier_ids}"
    )

    # pct_of_limit should reflect ~500%
    free_outlier = next(o for o in outliers if o["org_id"] == free_id)
    assert free_outlier["pct_of_limit"] >= 450.0, (
        f"Expected ~500% of limit, got {free_outlier['pct_of_limit']}%"
    )


@pytest.mark.asyncio
async def test_cross_tenant_visibility(db_conn, orgs):
    """bypass_tenant_scope sees all orgs — no accidental RLS filter applied."""
    from app.repositories.cost_metrics_repo import db_per_restaurant_costs
    from app.services.tenant_context import bypass_tenant_scope

    free_id, basic_id, pro_id = orgs
    today = date.today()

    # Seed all three test orgs
    for org_id, tokens in [(free_id, 1_000), (basic_id, 2_000), (pro_id, 3_000)]:
        await _set_org_scope(db_conn, org_id)
        await _seed_tokens(db_conn, org_id, today, tokens)

    with bypass_tenant_scope("internal_cost_dashboard"):
        rows = await db_per_restaurant_costs(today, today, limit=200)

    visible_ids = {r["org_id"] for r in rows}

    # All three test orgs must be visible in a single cross-tenant query
    assert free_id  in visible_ids, "Free org not visible under bypass scope"
    assert basic_id in visible_ids, "Basic org not visible under bypass scope"
    assert pro_id   in visible_ids, "Pro org not visible under bypass scope"
