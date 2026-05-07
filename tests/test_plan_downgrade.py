"""
tests/test_plan_downgrade.py
================================
Integration tests for the plan downgrade flow (0075_pending_plan_downgrade).

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Migration 0075 must be applied to the test DB
  - Uses _ConnProxy / _PoolShim pattern (matches test_plan_limits_repo.py)

Test matrix
-----------
  test_request_downgrade_sets_fields       — 3 pending_* cols populated, effective ~7d
  test_request_downgrade_wrong_direction   — Pulso→Restaurante raises ValueError (not a downgrade)
  test_request_downgrade_unknown_plan      — unknown plan_code raises ValueError
  test_request_downgrade_alien_location    — location_id from another org raises ValueError
  test_cancel_downgrade_clears_fields      — cancel returns True and clears the 3 cols
  test_cancel_downgrade_noop               — cancel when nothing pending returns False
  test_get_pending_downgrade_returns_state — returns dict with pending fields after request
  test_get_pending_downgrade_none          — returns None when no pending downgrade
  test_apply_due_processes_overdue         — org with effective_at in past gets processed
  test_apply_due_leaves_future_alone       — org with effective_at in future not processed
  test_tenant_isolation_pending            — org A's pending invisible to org B scope
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Proxy/Shim (same pattern as test_plan_limits_repo.py) ────────────────────


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


async def _set_scope(conn, org_id: int):
    """Set the RLS GUC for the given org_id."""
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


@pytest.fixture
async def two_orgs(db_conn):
    """Create two orgs with Pro plans and one location each. Returns (org_a_id, org_b_id, loc_a_id, loc_b_id)."""
    # Org A — starts on 'pro'
    await _set_scope(db_conn, 1)  # temporary scope for INSERT (WITH CHECK requires GUC)
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug, plan_code) VALUES ($1, $2, $3) RETURNING id",
        "DowngradeTestOrgA", "downgrade-test-org-a", "pro",
    )
    org_a = row_a["id"]

    await _set_scope(db_conn, org_a)
    loc_a = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, slug) VALUES ($1, $2, $3) RETURNING id",
        org_a, "Sede Principal A", "sede-a",
    )

    # Org B — starts on 'pro'
    await _set_scope(db_conn, 1)
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug, plan_code) VALUES ($1, $2, $3) RETURNING id",
        "DowngradeTestOrgB", "downgrade-test-org-b", "pro",
    )
    org_b = row_b["id"]

    await _set_scope(db_conn, org_b)
    loc_b = await db_conn.fetchval(
        "INSERT INTO locations (org_id, name, slug) VALUES ($1, $2, $3) RETURNING id",
        org_b, "Sede Principal B", "sede-b",
    )

    return org_a, org_b, loc_a, loc_b


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_downgrade_sets_fields(db_conn, two_orgs):
    """Requesting a downgrade populates the three pending_* columns.

    effective_at must be approximately NOW()+7d (within ±5min tolerance).
    """
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_request_downgrade, db_get_pending_downgrade

    org_a, _, loc_a, _ = two_orgs

    with tenant_scope(org_a):
        result = await db_request_downgrade(org_a, "pulso", loc_a)

    assert result["pending_plan_code"] == "pulso"
    assert result["pending_kept_location_id"] == loc_a

    # effective_at should be ~7 days from now (±5 minutes)
    eff_raw = result["pending_plan_effective_at"]
    # _row_to_dict converts datetime to isoformat string
    eff = datetime.fromisoformat(eff_raw.replace("Z", "+00:00")) if isinstance(eff_raw, str) else eff_raw
    if eff.tzinfo is None:
        eff = eff.replace(tzinfo=timezone.utc)
    expected = datetime.now(tz=timezone.utc) + timedelta(days=7)
    diff = abs((eff - expected).total_seconds())
    assert diff < 300, f"effective_at delta {diff}s exceeds 5 min tolerance"

    # Verify via db_get_pending_downgrade
    with tenant_scope(org_a):
        pending = await db_get_pending_downgrade(org_a)
    assert pending is not None
    assert pending["pending_plan_code"] == "pulso"


@pytest.mark.asyncio
async def test_request_downgrade_wrong_direction(db_conn, two_orgs):
    """Requesting a plan that is equal or larger than current raises ValueError."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_request_downgrade

    org_a, _, loc_a, _ = two_orgs
    # org_a is on 'pro' — requesting 'cadena' (larger) must be rejected
    with pytest.raises(ValueError, match="Downgrade only"):
        with tenant_scope(org_a):
            await db_request_downgrade(org_a, "cadena", loc_a)

    # Same plan as current ('pro') also rejected
    with pytest.raises(ValueError, match="Downgrade only"):
        with tenant_scope(org_a):
            await db_request_downgrade(org_a, "pro", loc_a)


@pytest.mark.asyncio
async def test_request_downgrade_unknown_plan(db_conn, two_orgs):
    """An unrecognised plan_code raises ValueError."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_request_downgrade

    org_a, _, loc_a, _ = two_orgs
    with pytest.raises(ValueError, match="Unknown plan_code"):
        with tenant_scope(org_a):
            await db_request_downgrade(org_a, "nonexistent_xyz", loc_a)


@pytest.mark.asyncio
async def test_request_downgrade_alien_location(db_conn, two_orgs):
    """kept_location_id from another org raises ValueError."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_request_downgrade

    org_a, _, loc_a, loc_b = two_orgs
    # loc_b belongs to org_b — should be rejected for org_a
    with pytest.raises(ValueError, match="does not belong to org"):
        with tenant_scope(org_a):
            await db_request_downgrade(org_a, "pulso", loc_b)


@pytest.mark.asyncio
async def test_cancel_downgrade_clears_fields(db_conn, two_orgs):
    """Cancel after request returns True and clears the pending_* columns."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import (
        db_request_downgrade,
        db_cancel_downgrade,
        db_get_pending_downgrade,
    )

    org_a, _, loc_a, _ = two_orgs
    with tenant_scope(org_a):
        await db_request_downgrade(org_a, "pulso", loc_a)

    with tenant_scope(org_a):
        existed = await db_cancel_downgrade(org_a)
    assert existed is True

    with tenant_scope(org_a):
        pending = await db_get_pending_downgrade(org_a)
    assert pending is None


@pytest.mark.asyncio
async def test_cancel_downgrade_noop(db_conn, two_orgs):
    """Cancelling when no pending downgrade exists returns False."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_cancel_downgrade

    org_a, _, _, _ = two_orgs
    with tenant_scope(org_a):
        existed = await db_cancel_downgrade(org_a)
    assert existed is False


@pytest.mark.asyncio
async def test_get_pending_downgrade_none(db_conn, two_orgs):
    """db_get_pending_downgrade returns None when no pending downgrade set."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import db_get_pending_downgrade

    org_a, _, _, _ = two_orgs
    with tenant_scope(org_a):
        result = await db_get_pending_downgrade(org_a)
    assert result is None


@pytest.mark.asyncio
async def test_apply_due_processes_overdue(db_conn, two_orgs):
    """db_apply_due_downgrades applies orgs whose effective_at is in the past."""
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.plan_limits_repo import db_apply_due_downgrades

    org_a, _, loc_a, _ = two_orgs

    # Manually set a past effective_at — bypass RLS for this setup write
    with bypass_tenant_scope("test_setup_overdue_downgrade"):
        await db_conn.execute(
            """
            UPDATE organizations
               SET pending_plan_code         = 'pulso',
                   pending_plan_effective_at = NOW() - INTERVAL '1 hour',
                   pending_kept_location_id  = $2
             WHERE id = $1
            """,
            org_a, loc_a,
        )

    with bypass_tenant_scope("test_apply_due"):
        processed = await db_apply_due_downgrades()

    processed_ids = [r.get("id") for r in processed]
    assert org_a in processed_ids, f"org_a not in processed: {processed_ids}"

    # plan_code should now be 'pulso', pending fields cleared
    row = await db_conn.fetchrow(
        "SELECT plan_code, pending_plan_code FROM organizations WHERE id = $1",
        org_a,
    )
    assert row["plan_code"] == "pulso"
    assert row["pending_plan_code"] is None


@pytest.mark.asyncio
async def test_apply_due_leaves_future_alone(db_conn, two_orgs):
    """db_apply_due_downgrades does NOT process orgs with future effective_at."""
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.plan_limits_repo import db_apply_due_downgrades

    org_a, _, loc_a, _ = two_orgs

    # Set a future effective_at
    with bypass_tenant_scope("test_setup_future_downgrade"):
        await db_conn.execute(
            """
            UPDATE organizations
               SET pending_plan_code         = 'pulso',
                   pending_plan_effective_at = NOW() + INTERVAL '7 days',
                   pending_kept_location_id  = $2
             WHERE id = $1
            """,
            org_a, loc_a,
        )

    with bypass_tenant_scope("test_apply_due_future"):
        processed = await db_apply_due_downgrades()

    processed_ids = [r.get("id") for r in processed]
    assert org_a not in processed_ids, f"org_a should NOT have been processed yet"

    # pending_plan_code must still be set
    row = await db_conn.fetchrow(
        "SELECT pending_plan_code FROM organizations WHERE id = $1",
        org_a,
    )
    assert row["pending_plan_code"] == "pulso"


@pytest.mark.asyncio
async def test_tenant_isolation_pending(db_conn, two_orgs):
    """org A's pending downgrade is not visible under org B's tenant scope."""
    from app.services.tenant_context import tenant_scope
    from app.repositories.plan_limits_repo import (
        db_request_downgrade,
        db_get_pending_downgrade,
    )

    org_a, org_b, loc_a, _ = two_orgs

    with tenant_scope(org_a):
        await db_request_downgrade(org_a, "pulso", loc_a)

    # Reading from org_b scope must return None (RLS enforced)
    with tenant_scope(org_b):
        pending_b = await db_get_pending_downgrade(org_b)
    assert pending_b is None, "org B must not see org A's pending downgrade"

    # Confirm org_a can still see its own
    with tenant_scope(org_a):
        pending_a = await db_get_pending_downgrade(org_a)
    assert pending_a is not None
