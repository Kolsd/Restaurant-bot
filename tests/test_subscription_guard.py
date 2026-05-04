"""
tests/test_subscription_guard.py
=================================
Coverage matrix for subscription_repo + subscription_guard.

Pure (no DB):
  test_resolve_limits_uses_plan_default
  test_resolve_limits_features_override
  test_resolve_limits_unknown_plan_falls_back_to_basic
  test_features_dict_handles_string_jsonb
  test_features_dict_handles_garbage_string
  test_enforce_invoice_limit_unlimited_plan_no_raise

Integration (require TEST_DATABASE_URL):
  test_db_increment_tokens_concurrent
  test_db_increment_invoices_atomic
  test_db_increment_orders_returns_monthly_total
  test_get_usage_summary_empty_tenant
  test_enforce_invoice_limit_under_cap_no_raise
  test_enforce_invoice_limit_at_cap_raises
  test_enforce_token_limit_projects_additional
  test_enforce_order_limit_monthly_aggregate
  test_subscription_usage_endpoint_shape
"""

import os
import json
import pytest
import asyncpg
from datetime import datetime, timedelta, timezone

from app.repositories import subscription_repo
from app.repositories.subscription_repo import _resolve_limits, _DEFAULT_LIMITS
from app.services.database import UsageLimitExceeded
from app.services.subscription_guard import (
    enforce_invoice_limit,
    enforce_token_limit,
    enforce_order_limit,
    _features_dict,
    _plan,
)


# ── Pure tests (no DB) ───────────────────────────────────────────────────────


def test_resolve_limits_uses_plan_default():
    """Empty features.plan_limits + plan='basic' returns the basic defaults."""
    out = _resolve_limits({}, "basic")
    assert out["daily_tokens"]   == _DEFAULT_LIMITS["basic"]["daily_tokens"]
    assert out["daily_invoices"] == _DEFAULT_LIMITS["basic"]["daily_invoices"]
    assert out["monthly_orders"] == _DEFAULT_LIMITS["basic"]["monthly_orders"]


def test_resolve_limits_features_override():
    """features.plan_limits values override plan defaults."""
    feats = {"plan_limits": {"daily_tokens": 100, "monthly_orders": 9999}}
    out = _resolve_limits(feats, "basic")
    assert out["daily_tokens"]   == 100        # overridden
    assert out["daily_invoices"] == _DEFAULT_LIMITS["basic"]["daily_invoices"]  # default
    assert out["monthly_orders"] == 9999       # overridden


def test_resolve_limits_unknown_plan_falls_back_to_basic():
    """Unknown plan strings fall back to 'basic' (the safe middle)."""
    out = _resolve_limits({}, "platinum_extreme_plus")
    assert out["daily_tokens"]   == _DEFAULT_LIMITS["basic"]["daily_tokens"]
    assert out["daily_invoices"] == _DEFAULT_LIMITS["basic"]["daily_invoices"]
    assert out["monthly_orders"] == _DEFAULT_LIMITS["basic"]["monthly_orders"]


def test_resolve_limits_none_inputs_safe():
    """None features/plan don't crash; defaults to basic limits."""
    out = _resolve_limits(None, None)
    assert out["daily_tokens"] == _DEFAULT_LIMITS["basic"]["daily_tokens"]


def test_resolve_limits_enterprise_unlimited():
    """Enterprise plan returns -1 (unlimited) for all keys."""
    out = _resolve_limits({}, "enterprise")
    assert out["daily_tokens"] == -1
    assert out["daily_invoices"] == -1
    assert out["monthly_orders"] == -1


def test_features_dict_handles_string_jsonb():
    """features as a JSON string (some asyncpg paths) gets parsed."""
    rest = {"features": '{"plan_limits": {"daily_tokens": 42}}'}
    out = _features_dict(rest)
    assert out == {"plan_limits": {"daily_tokens": 42}}


def test_features_dict_handles_garbage_string():
    """Non-JSON string returns {} (don't crash)."""
    rest = {"features": "this is not json"}
    out = _features_dict(rest)
    assert out == {}


def test_features_dict_handles_none():
    """features=None returns {}."""
    out = _features_dict({"features": None})
    assert out == {}


def test_plan_default_is_basic():
    """Missing subscription_plan defaults to 'basic'."""
    assert _plan({}) == "basic"
    assert _plan({"subscription_plan": None}) == "basic"
    assert _plan({"subscription_plan": "pro"}) == "pro"


@pytest.mark.asyncio
async def test_enforce_invoice_limit_unlimited_plan_no_raise():
    """Enterprise (cap=-1) short-circuits before any DB read."""
    # No DB call should happen because cap=-1 exits early. We verify by
    # passing an org_id that doesn't exist — if the function tried to read
    # the DB it would either fail or return zeros (and not raise).
    rest = {"subscription_plan": "enterprise", "features": {}}
    # Should NOT raise and should NOT need a DB connection.
    await enforce_invoice_limit(999_999_999, rest)


@pytest.mark.asyncio
async def test_enforce_token_limit_unlimited_plan_no_raise():
    rest = {"subscription_plan": "enterprise", "features": {}}
    await enforce_token_limit(999_999_999, rest, additional=10**9)


@pytest.mark.asyncio
async def test_enforce_order_limit_unlimited_plan_no_raise():
    rest = {"subscription_plan": "enterprise", "features": {}}
    await enforce_order_limit(999_999_999, rest)


# ── Integration tests (require TEST_DATABASE_URL) ─────────────────────────────


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_skip_no_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


class _ConnProxy:
    """asyncpg-compatible proxy around a Connection (works around __slots__)."""

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


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
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
    """Create two isolated orgs and return (org_a_id, org_b_id)."""
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "SubGuardOrgA", "subguard-org-a",
    )
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "SubGuardOrgB", "subguard-org-b",
    )
    return row_a["id"], row_b["id"]


async def _set_scope(conn, org_id):
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


# ── Repo tests ────────────────────────────────────────────────────────────────


@_skip_no_db
@pytest.mark.asyncio
async def test_db_increment_tokens_concurrent(db_conn, org_ids):
    """Two sequential increments sum correctly via ON CONFLICT idempotency."""
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        v1 = await subscription_repo.db_increment_tokens(org_a, 100)
        v2 = await subscription_repo.db_increment_tokens(org_a, 50)
    assert v1 == 100
    assert v2 == 150


@_skip_no_db
@pytest.mark.asyncio
async def test_db_increment_invoices_atomic(db_conn, org_ids):
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        v1 = await subscription_repo.db_increment_invoices(org_a)
        v2 = await subscription_repo.db_increment_invoices(org_a)
        v3 = await subscription_repo.db_increment_invoices(org_a)
    assert v1 == 1
    assert v2 == 2
    assert v3 == 3


@_skip_no_db
@pytest.mark.asyncio
async def test_db_increment_orders_returns_monthly_total(db_conn, org_ids):
    """orders_count increments today's row; the return value aggregates the month."""
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Seed one row earlier in the current month.
    earlier_in_month = datetime.now(timezone.utc).date().replace(day=1)
    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_tokens, total_invoices, orders_count)
           VALUES ($1, $1, $2, 0, 0, 5)""",
        org_a, earlier_in_month,
    )

    with tenant_scope(org_a):
        monthly = await subscription_repo.db_increment_orders(org_a)
    # 5 (seed) + 1 (this call) = 6
    assert monthly == 6


@_skip_no_db
@pytest.mark.asyncio
async def test_get_usage_summary_empty_tenant(db_conn, org_ids):
    """Fresh org returns zeros — no 500."""
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    with tenant_scope(org_a):
        summary = await subscription_repo.db_get_usage_summary(org_a)
    assert summary == {
        "today": {"tokens_used": 0, "invoices_used": 0, "orders_count": 0},
        "month": {"orders_count": 0},
    }


@_skip_no_db
@pytest.mark.asyncio
async def test_get_usage_summary_after_seed(db_conn, org_ids):
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_tokens, total_invoices, orders_count)
           VALUES ($1, $1, CURRENT_DATE, 1234, 7, 3)""",
        org_a,
    )

    with tenant_scope(org_a):
        summary = await subscription_repo.db_get_usage_summary(org_a)
    assert summary["today"] == {"tokens_used": 1234, "invoices_used": 7, "orders_count": 3}
    assert summary["month"]["orders_count"] == 3


# ── Guard tests ───────────────────────────────────────────────────────────────


@_skip_no_db
@pytest.mark.asyncio
async def test_enforce_invoice_limit_under_cap_no_raise(db_conn, org_ids):
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Seed: 3 invoices today, plan basic (cap=50). Should NOT raise.
    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_invoices)
           VALUES ($1, $1, CURRENT_DATE, 3)""",
        org_a,
    )
    rest = {"subscription_plan": "basic", "features": {}}
    with tenant_scope(org_a):
        await enforce_invoice_limit(org_a, rest)  # no raise


@_skip_no_db
@pytest.mark.asyncio
async def test_enforce_invoice_limit_at_cap_raises(db_conn, org_ids):
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    # Seed: invoice count == cap → must raise.
    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_invoices)
           VALUES ($1, $1, CURRENT_DATE, 5)""",
        org_a,
    )
    # Use a custom override for a low cap to keep the assertion precise.
    rest = {"subscription_plan": "basic",
            "features": {"plan_limits": {"daily_invoices": 5}}}
    with tenant_scope(org_a):
        with pytest.raises(UsageLimitExceeded) as exc:
            await enforce_invoice_limit(org_a, rest)
    assert exc.value.resource == "facturas"
    assert exc.value.used == 5
    assert exc.value.limit == 5


@_skip_no_db
@pytest.mark.asyncio
async def test_enforce_token_limit_projects_additional(db_conn, org_ids):
    """used=4500 + additional=600, cap=5000 → raises (5100 > 5000)."""
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_tokens)
           VALUES ($1, $1, CURRENT_DATE, 4500)""",
        org_a,
    )
    rest = {"subscription_plan": "free", "features": {}}  # free cap=5000
    with tenant_scope(org_a):
        # additional=400: 4500+400=4900, under cap → no raise
        await enforce_token_limit(org_a, rest, additional=400)
        # additional=600: 4500+600=5100 > cap → raises
        with pytest.raises(UsageLimitExceeded) as exc:
            await enforce_token_limit(org_a, rest, additional=600)
    assert exc.value.resource == "tokens"


@_skip_no_db
@pytest.mark.asyncio
async def test_enforce_order_limit_monthly_aggregate(db_conn, org_ids):
    """3 days × 10 orders, cap=25 → raises on the 26th."""
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_scope(db_conn, org_a)

    today = datetime.now(timezone.utc).date()
    # Seed 3 same-month days (today, today-1, today-2). All in the same month
    # for any day-of-month >= 3 — guaranteed for our test fixture.
    if today.day < 3:
        # Skip if running on day 1 or 2 of the month (rare but possible).
        pytest.skip("Test requires day-of-month >= 3 to guarantee same-month rows")
    for offset, count in enumerate([10, 10, 10]):
        d = today - timedelta(days=offset)
        await db_conn.execute(
            """INSERT INTO subscription_usage
                 (org_id, restaurant_id, usage_date, orders_count)
               VALUES ($1, $1, $2, $3)""",
            org_a, d, count,
        )

    # Custom cap=25 via features override
    rest = {"subscription_plan": "basic",
            "features": {"plan_limits": {"monthly_orders": 25}}}
    with tenant_scope(org_a):
        with pytest.raises(UsageLimitExceeded) as exc:
            await enforce_order_limit(org_a, rest)
    assert exc.value.resource == "órdenes"
    assert exc.value.used == 30  # 3×10
    assert exc.value.limit == 25


@_skip_no_db
@pytest.mark.asyncio
async def test_tenant_isolation_usage_summary(db_conn, org_ids):
    """Org A usage doesn't bleed into Org B's summary."""
    from app.services.tenant_context import tenant_scope

    org_a, org_b = org_ids
    # Seed for org A under its scope
    await _set_scope(db_conn, org_a)
    await db_conn.execute(
        """INSERT INTO subscription_usage
             (org_id, restaurant_id, usage_date, total_tokens)
           VALUES ($1, $1, CURRENT_DATE, 9999)""",
        org_a,
    )

    # Switch to org B's scope and read
    await _set_scope(db_conn, org_b)
    with tenant_scope(org_b):
        summary_b = await subscription_repo.db_get_usage_summary(org_b)
    assert summary_b["today"]["tokens_used"] == 0
