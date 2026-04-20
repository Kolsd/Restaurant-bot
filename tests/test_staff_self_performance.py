"""
tests/test_staff_self_performance.py

Tests for GET /api/staff/self/performance?days=30  (Sprint Z).

Unit tests (no live DB, mocked pool):
  - test_performance_zero_metrics         no sessions or shifts → all zeros
  - test_performance_full_metrics         sessions + tips present → correct values
  - test_performance_nps_always_null      nps_average is always null (no FK link)
  - test_performance_days_param           days=7 uses a shorter look-back window

Integration test (requires TEST_DATABASE_URL):
  - test_performance_integration_shape    seeds org+staff, validates response shape
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tenant_context import tenant_scope

pytestmark = pytest.mark.asyncio

_INTEGRATION_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_SKIP_INTEGRATION = pytest.mark.skipif(
    not _INTEGRATION_URL,
    reason="Set TEST_DATABASE_URL to run integration tests",
)


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch    = AsyncMock(return_value=[])
    conn.execute  = AsyncMock(return_value="UPDATE 1")
    _txn = MagicMock()
    _txn.__aenter__ = AsyncMock(return_value=_txn)
    _txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=_txn)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__  = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_row(d: dict):
    row = MagicMock()
    row.__iter__    = lambda s: iter(d.items())
    row.keys        = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get         = lambda k, default=None: d.get(k, default)
    return row


# ════════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════════════════════════════════════

async def test_performance_zero_metrics():
    """When there are no sessions or shifts, all numeric metrics are zero."""
    conn = _make_conn()
    # Both fetchrow calls return zero-count rows.
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row({"tables_served": 0, "avg_ticket": 0}),  # sessions query
        _make_row({"shift_count": 0}),                      # shifts query
    ])
    pool = _make_pool(conn)

    # Mock db_calculate_tips_for_staff to return zero.
    zero_tips = {"amount": 0.0, "count": 0}

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with patch(
            "app.repositories.staff_repo.db_calculate_tips_for_staff",
            AsyncMock(return_value=zero_tips),
        ):
            with tenant_scope(1):
                from app.repositories.staff_repo import db_get_staff_performance
                result = await db_get_staff_performance(
                    org_id=1,
                    staff_id=str(uuid.uuid4()),
                    days=30,
                )

    assert result["period_days"] == 30
    m = result["metrics"]
    assert m["tables_served"]     == 0
    assert m["avg_ticket"]        == 0
    assert m["tips_total"]        == 0.0
    assert m["avg_tip_per_shift"] == 0.0
    assert m["nps_average"]       is None
    assert m["nps_response_count"] == 0


async def test_performance_full_metrics():
    """When sessions and tips exist, computed values are correct."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row({"tables_served": 87, "avg_ticket": 62400}),
        _make_row({"shift_count": 30}),
    ])
    pool = _make_pool(conn)

    tip_data = {"amount": 540000.0, "count": 87}

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with patch(
            "app.repositories.staff_repo.db_calculate_tips_for_staff",
            AsyncMock(return_value=tip_data),
        ):
            with tenant_scope(5):
                from app.repositories.staff_repo import db_get_staff_performance
                result = await db_get_staff_performance(
                    org_id=5,
                    staff_id=str(uuid.uuid4()),
                    days=30,
                )

    m = result["metrics"]
    assert m["tables_served"] == 87
    assert m["avg_ticket"]    == 62400
    assert m["tips_total"]    == 540000.0
    # avg_tip_per_shift = 540000 / 30 = 18000.0
    assert m["avg_tip_per_shift"] == 18000.0


async def test_performance_nps_always_null():
    """nps_average is always None — nps_responses has no session_id FK today."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row({"tables_served": 5, "avg_ticket": 30000}),
        _make_row({"shift_count": 5}),
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with patch(
            "app.repositories.staff_repo.db_calculate_tips_for_staff",
            AsyncMock(return_value={"amount": 10000.0, "count": 5}),
        ):
            with tenant_scope(2):
                from app.repositories.staff_repo import db_get_staff_performance
                result = await db_get_staff_performance(
                    org_id=2,
                    staff_id=str(uuid.uuid4()),
                    days=30,
                )

    assert result["metrics"]["nps_average"]        is None
    assert result["metrics"]["nps_response_count"] == 0


async def test_performance_days_param():
    """days parameter is echoed back in period_days and changes the look-back."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(side_effect=[
        _make_row({"tables_served": 10, "avg_ticket": 20000}),
        _make_row({"shift_count": 7}),
    ])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with patch(
            "app.repositories.staff_repo.db_calculate_tips_for_staff",
            AsyncMock(return_value={"amount": 70000.0, "count": 10}),
        ):
            with tenant_scope(3):
                from app.repositories.staff_repo import db_get_staff_performance
                result = await db_get_staff_performance(
                    org_id=3,
                    staff_id=str(uuid.uuid4()),
                    days=7,
                )

    assert result["period_days"] == 7
    assert result["metrics"]["avg_tip_per_shift"] == pytest.approx(10000.0, rel=1e-3)


# ════════════════════════════════════════════════════════════════════════════════
# INTEGRATION TEST (requires TEST_DATABASE_URL)
# ════════════════════════════════════════════════════════════════════════════════

class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


class _ConnProxy:
    """Thin wrapper to avoid asyncpg.Connection.__slots__ issues with MagicMock."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture
async def db_conn():
    """Isolated asyncpg connection rolled back after each test."""
    import asyncpg
    conn = await asyncpg.connect(_INTEGRATION_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


async def _insert_org(conn) -> tuple[int, int]:
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number, features)
           VALUES ($1, $2, $3::jsonb)
           RETURNING id""",
        f"PerfOrg-{uuid.uuid4().hex[:8]}",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
        json.dumps({"tip_distribution": {"mesero": 100}}),
    )
    location_id = await conn.fetchval(
        """INSERT INTO locations (org_id, name, code, active, timezone)
           VALUES ($1, 'Sede Test', 'main', true, 'America/Bogota')
           RETURNING id""",
        org_id,
    )
    return org_id, location_id


async def _insert_staff(conn, org_id: int, role: str = "mesero") -> str:
    sid = uuid.uuid4()
    uname = f"test_{uuid.uuid4().hex[:10]}"
    await conn.execute(
        """INSERT INTO staff (id, org_id, name, role, pin, username)
           VALUES ($1, $2, 'Test Staff', $3, '', $4)""",
        sid, org_id, role, uname,
    )
    return str(sid)


@_SKIP_INTEGRATION
async def test_performance_integration_shape(db_conn):
    """Seeds org + staff; validates that db_get_staff_performance returns correct shape.

    No table sessions or shifts are seeded — all metrics should be zero, but the
    shape must be fully present and nps_average must be null.
    """
    org_id, location_id = await _insert_org(db_conn)
    staff_id = await _insert_staff(db_conn, org_id, role="mesero")

    proxy = _ConnProxy(db_conn)
    shim  = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        with tenant_scope(org_id):
            from app.repositories.staff_repo import db_get_staff_performance
            result = await db_get_staff_performance(
                org_id=org_id,
                staff_id=staff_id,
                days=30,
            )

    # Shape assertions
    assert "period_days" in result
    assert result["period_days"] == 30
    assert "metrics" in result
    m = result["metrics"]
    assert "tables_served"      in m
    assert "avg_ticket"         in m
    assert "tips_total"         in m
    assert "avg_tip_per_shift"  in m
    assert "nps_average"        in m
    assert "nps_response_count" in m

    # Values: all zero (no data seeded)
    assert m["tables_served"]     == 0
    assert m["avg_ticket"]        == 0
    assert m["tips_total"]        == 0.0
    assert m["avg_tip_per_shift"] == 0.0
    assert m["nps_average"]       is None
    assert m["nps_response_count"] == 0
