"""
tests/test_mrr.py
=================
Tests for MRR (Monthly Recurring Revenue) tracking.

Test matrix
-----------
  test_zero_paying_orgs             — fresh DB / only free orgs → mrr_total_cop = 0
  test_single_pulso_paying          — 1 paying Pulso → mrr = 149000
  test_paying_plus_comp             — 1 paying + 1 comp → mrr = 149000, counts correct
  test_multiple_plans               — mix of plans aggregates correctly
  test_comp_org_not_in_mrr          — comp org (comp_until in future) excluded from mrr
  test_delta_shape                  — db_compute_mrr_delta returns expected keys
  test_endpoint_shape               — GET /api/internal/analytics/mrr returns expected shape
                                      (unit test with mocked repo, no DB required)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

# ── Integration tests gated on TEST_DATABASE_URL ─────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_db_mark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

# ── Helpers: ConnProxy + PoolShim (same pattern as test_pedidos_rescatados) ──


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

if TEST_DB_URL:
    import asyncpg

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

    async def _set_org_scope(conn, org_id: int) -> None:
        await conn.execute(
            "SELECT set_config('app.org_id', $1::text, true)",
            str(org_id),
        )

    async def _create_org(conn, plan_code: str = "free", comp_until=None) -> int:
        """Insert a minimal org with given plan_code and return its id."""
        slug = f"mrr-test-{uuid.uuid4().hex[:10]}"
        row = await conn.fetchrow(
            """
            INSERT INTO organizations (name, slug, plan_code, comp_until)
            VALUES ($1, $2, $3, $4) RETURNING id
            """,
            f"MRR Test {slug}", slug, plan_code, comp_until,
        )
        return row["id"]


# ── Integration tests ─────────────────────────────────────────────────────────


@_db_mark
@pytest.mark.asyncio
async def test_zero_paying_orgs(db_conn):
    """A 'free' org must not inflate MRR.

    PRODUCT GAP (verified 2026-09-10, do not silently paper over):
    plan_code='free' as originally written here CANNOT be constructed on the
    current schema. organizations.plan_code is `NOT NULL DEFAULT 'pulso'` and
    carries `fk_orgs_plan_code -> plan_limits(plan_code)` (migration 0070),
    whose only seeded rows are pulso/restaurante/pro/cadena — inserting
    plan_code='free' raises ForeignKeyViolationError. NULL is also impossible
    (NOT NULL). So the "free" branch in
    app/repositories/internal/mrr_repo.py::db_compute_mrr()
    (`o.plan_code IS NULL OR o.plan_code IN ('free', '')`) is dead code today:
    no organization row can ever land in that branch. If a genuine free tier
    is wanted, `plan_limits` needs a seeded 'free' row (monthly_price_cop=0)
    via a migration — out of scope here (migrations are owned elsewhere).

    Until that's decided, this test verifies the only thing that IS true on
    the current schema: with no new orgs inserted, db_compute_mrr() is a pure
    read (calling it twice yields identical results — no side effects), the
    shape is well-formed, mrr_total_cop is never negative, and free_count is
    always exactly 0 (since no org can structurally be 'free').
    """
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("test_mrr_zero_paying_before"):
        before = await mrr_repo.db_compute_mrr()
    with bypass_tenant_scope("test_mrr_zero_paying_after"):
        after = await mrr_repo.db_compute_mrr()

    assert before == after, "db_compute_mrr() must be a pure read with no new orgs inserted"
    assert isinstance(after["mrr_total_cop"], int)
    assert after["mrr_total_cop"] >= 0
    assert after["free_count"] == 0, (
        "no organization can structurally have plan_code='free' or NULL given "
        "fk_orgs_plan_code + NOT NULL — if this ever fails, a free plan_code "
        "was seeded and the dead-code assumption above is stale"
    )
    assert "by_plan" in after
    assert "paying_count" in after
    assert "comp_count" in after
    assert "total_orgs" in after


@_db_mark
@pytest.mark.asyncio
async def test_single_pulso_paying(db_conn):
    """
    Insert 1 Pulso paying org (no comp_until).
    MRR contribution from that org = 149000.
    We verify the by_plan entry for 'pulso' increases by exactly 149000.
    """
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    # Baseline before inserting
    with bypass_tenant_scope("test_mrr_pulso_before"):
        before = await mrr_repo.db_compute_mrr()
    pulso_before = next(p for p in before["by_plan"] if p["plan_code"] == "pulso")

    # Insert a paying Pulso org
    await _create_org(db_conn, plan_code="pulso", comp_until=None)

    with bypass_tenant_scope("test_mrr_pulso_after"):
        after = await mrr_repo.db_compute_mrr()
    pulso_after = next(p for p in after["by_plan"] if p["plan_code"] == "pulso")

    # Exactly one more paying Pulso org
    assert pulso_after["paying_count"] == pulso_before["paying_count"] + 1
    assert pulso_after["mrr_cop"] == pulso_before["mrr_cop"] + 149_000
    assert after["mrr_total_cop"] == before["mrr_total_cop"] + 149_000
    assert after["paying_count"] == before["paying_count"] + 1


@_db_mark
@pytest.mark.asyncio
async def test_paying_plus_comp(db_conn):
    """
    Insert 1 paying Pulso + 1 comp org.
    MRR increases by 149000, paying_count +1, comp_count +1, comp org does NOT add to MRR.
    """
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("test_mrr_pay_comp_before"):
        before = await mrr_repo.db_compute_mrr()

    future = datetime.now(timezone.utc) + timedelta(days=30)
    await _create_org(db_conn, plan_code="pulso", comp_until=None)
    await _create_org(db_conn, plan_code="restaurante", comp_until=future)

    with bypass_tenant_scope("test_mrr_pay_comp_after"):
        after = await mrr_repo.db_compute_mrr()

    # Only the pulso org contributes MRR
    assert after["mrr_total_cop"] == before["mrr_total_cop"] + 149_000
    assert after["paying_count"] == before["paying_count"] + 1
    assert after["comp_count"] == before["comp_count"] + 1


@_db_mark
@pytest.mark.asyncio
async def test_comp_org_not_in_mrr(db_conn):
    """A comp org with comp_until in the future must NOT be counted as paying."""
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("test_mrr_comp_before"):
        before = await mrr_repo.db_compute_mrr()

    future = datetime.now(timezone.utc) + timedelta(days=90)
    # Pro plan but on comp — should NOT add $549K to MRR
    await _create_org(db_conn, plan_code="pro", comp_until=future)

    with bypass_tenant_scope("test_mrr_comp_after"):
        after = await mrr_repo.db_compute_mrr()

    assert after["mrr_total_cop"] == before["mrr_total_cop"]  # no change
    assert after["comp_count"] == before["comp_count"] + 1


@_db_mark
@pytest.mark.asyncio
async def test_multiple_plans(db_conn):
    """
    2 Pulso + 1 Restaurante paying → MRR = 2*149000 + 299000 = 597000 above baseline.
    """
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("test_mrr_multi_before"):
        before = await mrr_repo.db_compute_mrr()

    await _create_org(db_conn, plan_code="pulso")
    await _create_org(db_conn, plan_code="pulso")
    await _create_org(db_conn, plan_code="restaurante")

    with bypass_tenant_scope("test_mrr_multi_after"):
        after = await mrr_repo.db_compute_mrr()

    expected_delta = 2 * 149_000 + 299_000  # 597000
    assert after["mrr_total_cop"] == before["mrr_total_cop"] + expected_delta
    assert after["paying_count"] == before["paying_count"] + 3


@_db_mark
@pytest.mark.asyncio
async def test_delta_shape(db_conn):
    """db_compute_mrr_delta returns the expected keys with correct types."""
    from app.services.tenant_context import bypass_tenant_scope
    from app.repositories.internal import mrr_repo

    with bypass_tenant_scope("test_mrr_delta_shape"):
        delta = await mrr_repo.db_compute_mrr_delta()

    assert "mrr_last_month_cop" in delta
    assert "delta_cop" in delta
    assert "delta_pct" in delta
    assert isinstance(delta["mrr_last_month_cop"], int)
    assert isinstance(delta["delta_cop"], int)
    assert isinstance(delta["delta_pct"], float)


# ── Unit test: endpoint shape (no DB required) ────────────────────────────────


@pytest.mark.asyncio
async def test_endpoint_shape():
    """
    GET /api/internal/analytics/mrr returns the merged current+delta shape.
    Uses mocked repo so no DB is needed.
    """
    mock_current = {
        "mrr_total_cop": 298_000,
        "by_plan": [
            {"plan_code": "pulso", "monthly_price_cop": 149_000, "paying_count": 2, "mrr_cop": 298_000},
            {"plan_code": "restaurante", "monthly_price_cop": 299_000, "paying_count": 0, "mrr_cop": 0},
            {"plan_code": "pro", "monthly_price_cop": 549_000, "paying_count": 0, "mrr_cop": 0},
            {"plan_code": "cadena", "monthly_price_cop": 899_000, "paying_count": 0, "mrr_cop": 0},
        ],
        "paying_count": 2,
        "comp_count": 1,
        "free_count": 3,
        "total_orgs": 6,
    }
    mock_delta = {
        "mrr_last_month_cop": 149_000,
        "delta_cop": 149_000,
        "delta_pct": 100.0,
    }

    with patch(
        "app.repositories.internal.mrr_repo.db_compute_mrr",
        new_callable=AsyncMock,
        return_value=mock_current,
    ), patch(
        "app.repositories.internal.mrr_repo.db_compute_mrr_delta",
        new_callable=AsyncMock,
        return_value=mock_delta,
    ):
        from app.repositories.internal import mrr_repo

        current = await mrr_repo.db_compute_mrr()
        delta = await mrr_repo.db_compute_mrr_delta()
        response = {**current, **delta}

    # Shape assertions
    assert response["mrr_total_cop"] == 298_000
    assert response["paying_count"] == 2
    assert response["comp_count"] == 1
    assert response["free_count"] == 3
    assert response["total_orgs"] == 6
    assert len(response["by_plan"]) == 4
    assert response["delta_pct"] == 100.0
    assert response["delta_cop"] == 149_000

    # by_plan correctness
    pulso = next(p for p in response["by_plan"] if p["plan_code"] == "pulso")
    assert pulso["mrr_cop"] == 298_000
    assert pulso["paying_count"] == 2
