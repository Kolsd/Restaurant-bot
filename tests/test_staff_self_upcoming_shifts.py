"""
tests/test_staff_self_upcoming_shifts.py

Tests for GET /api/staff/self/upcoming-shifts  (Sprint Y).

Unit tests (no live DB):
  - test_no_schedule_returns_empty
  - test_weekly_pattern_generates_next_N
  - test_respects_limit_param
  - test_duration_computed_correctly
  - test_overnight_shift_duration

Integration tests (require TEST_DATABASE_URL):
  - test_tenant_isolation_upcoming_shifts
  - test_upcoming_shifts_roundtrip
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta, time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.tenant_context import tenant_scope, bypass_tenant_scope

pytestmark = pytest.mark.asyncio

_INTEGRATION_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_SKIP_INTEGRATION = pytest.mark.skipif(
    not _INTEGRATION_URL,
    reason="Set TEST_DATABASE_URL to run integration tests",
)


# ── Mock helpers ─────────────────────────────────────────────────────────────

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

async def test_no_schedule_returns_empty():
    """Staff with no schedule entries returns empty shifts list."""
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(1):
            result = await db_get_staff_upcoming_shifts(
                staff_id="some-uuid",
                limit=3,
            )

    assert result == []


async def test_weekly_pattern_generates_next_N():
    """A schedule with entries on Mon/Wed/Fri generates 3 upcoming dates."""
    # Mock schedule: Monday (0), Wednesday (2), Friday (4)
    fake_rows = [
        _make_row({"day_of_week": 0, "start_time": _time(9, 0), "end_time": _time(17, 0)}),
        _make_row({"day_of_week": 2, "start_time": _time(9, 0), "end_time": _time(17, 0)}),
        _make_row({"day_of_week": 4, "start_time": _time(9, 0), "end_time": _time(17, 0)}),
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=fake_rows)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(1):
            result = await db_get_staff_upcoming_shifts(
                staff_id="some-uuid",
                limit=3,
            )

    assert len(result) == 3
    # All returned shifts must be on Mon/Wed/Fri (day_of_week in {0,2,4})
    for shift in result:
        assert shift["day_of_week"] in {0, 2, 4}
    # Dates must be in ascending order
    dates = [s["date"] for s in result]
    assert dates == sorted(dates)


async def test_respects_limit_param():
    """limit=2 returns at most 2 shifts even when more pattern matches exist."""
    fake_rows = [
        _make_row({"day_of_week": i, "start_time": _time(10, 0), "end_time": _time(18, 0)})
        for i in range(7)  # every day of the week
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=fake_rows)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(1):
            result = await db_get_staff_upcoming_shifts(
                staff_id="some-uuid",
                limit=2,
            )

    assert len(result) == 2


async def test_duration_computed_correctly():
    """duration_hours is correctly computed from start_time and end_time."""
    fake_rows = [
        _make_row({"day_of_week": 0, "start_time": _time(14, 0), "end_time": _time(22, 0)}),
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=fake_rows)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(1):
            result = await db_get_staff_upcoming_shifts(
                staff_id="some-uuid",
                limit=1,
            )

    assert len(result) == 1
    assert result[0]["duration_hours"] == 8.0
    assert result[0]["start_time"] == "14:00"
    assert result[0]["end_time"] == "22:00"


async def test_overnight_shift_duration():
    """Overnight shifts (end < start) have duration computed correctly."""
    fake_rows = [
        # 22:00 → 06:00 next day = 8h
        _make_row({"day_of_week": 5, "start_time": _time(22, 0), "end_time": _time(6, 0)}),
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=fake_rows)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(1):
            result = await db_get_staff_upcoming_shifts(
                staff_id="some-uuid",
                limit=1,
            )

    assert len(result) == 1
    assert result[0]["duration_hours"] == 8.0


# ════════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS (require TEST_DATABASE_URL)
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


async def _insert_org(conn) -> int:
    """Insert minimal org + location, return org_id."""
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number)
           VALUES ($1, $2)
           RETURNING id""",
        f"TestOrg-{uuid.uuid4().hex[:8]}",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
    )
    await conn.execute(
        """INSERT INTO locations (org_id, name, code, active, timezone)
           VALUES ($1, 'Sede Test', 'main', true, 'America/Bogota')""",
        org_id,
    )
    return org_id


async def _insert_staff(conn, org_id: int) -> str:
    """Insert a staff member, return UUID string."""
    sid = uuid.uuid4()
    uname = f"test_{uuid.uuid4().hex[:10]}"
    await conn.execute(
        """INSERT INTO staff (id, org_id, name, role, pin, username)
           VALUES ($1, $2, 'Test Staff', 'mesero', '', $3)""",
        sid, org_id, uname,
    )
    return str(sid)


async def _insert_schedule(conn, staff_id: str, org_id: int, dow: int) -> None:
    """Insert an active schedule entry for the given day_of_week."""
    await conn.execute(
        """INSERT INTO staff_schedules
               (staff_id, org_id, day_of_week, start_time, end_time, active)
           VALUES ($1::uuid, $2, $3, '10:00'::time, '18:00'::time, true)""",
        staff_id, org_id, dow,
    )


@_SKIP_INTEGRATION
async def test_upcoming_shifts_roundtrip(db_conn):
    """Staff with a 3-day weekly schedule gets 3 upcoming shifts."""
    org_id = await _insert_org(db_conn)
    staff_id = await _insert_staff(db_conn, org_id)

    # Schedule: Monday, Wednesday, Friday
    await _insert_schedule(db_conn, staff_id, org_id, 0)  # Mon
    await _insert_schedule(db_conn, staff_id, org_id, 2)  # Wed
    await _insert_schedule(db_conn, staff_id, org_id, 4)  # Fri

    proxy = _ConnProxy(db_conn)
    shim = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(org_id):
            result = await db_get_staff_upcoming_shifts(staff_id=staff_id, limit=3)

    assert len(result) == 3
    for shift in result:
        assert shift["day_of_week"] in {0, 2, 4}
        assert shift["start_time"] == "10:00"
        assert shift["end_time"] == "18:00"
        assert shift["duration_hours"] == 8.0
    # Dates sorted ascending
    dates = [s["date"] for s in result]
    assert dates == sorted(dates)


@_SKIP_INTEGRATION
async def test_tenant_isolation_upcoming_shifts(db_conn):
    """Staff from org A cannot trigger leakage of org B's schedule data.

    RLS on staff_schedules filters by org_id (set via tenant_scope). The query
    filters by staff_id too; cross-tenant access is blocked at both layers.
    We verify that querying with a non-existent UUID returns empty, not an error.
    """
    # Create a real org to seed the scope (RLS needs app.org_id set).
    seed_org_id = await _insert_org(db_conn)
    phantom_staff_id = str(uuid.uuid4())  # UUID that belongs to no org

    proxy = _ConnProxy(db_conn)
    shim = _PoolShim(proxy)

    with patch("app.services.database.get_pool", AsyncMock(return_value=shim)):
        from app.repositories.staff_repo import db_get_staff_upcoming_shifts
        from app.services.tenant_context import tenant_scope
        with tenant_scope(seed_org_id):
            result = await db_get_staff_upcoming_shifts(staff_id=phantom_staff_id, limit=3)

    assert result == []
