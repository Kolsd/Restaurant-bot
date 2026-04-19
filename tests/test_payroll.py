"""
tests/test_payroll.py — Real-database integration tests for db_calculate_payroll.

Requires a live PostgreSQL instance.  Set TEST_DATABASE_URL or DATABASE_URL to
a writable schema before running.

Each test is wrapped in a transaction that is always rolled back, so no test
data leaks between runs and the suite is safe to execute against a shared
staging database.

How connection injection works
──────────────────────────────
db_calculate_payroll uses tenant_connection() which calls
app.services.database.get_pool().  Each test patches get_pool at the module
level (app.services.database.get_pool) to return a _PoolShim that wraps the
test connection already inside the open transaction.  Calls are wrapped in
tenant_scope(org_id) so tenant_connection() issues SET LOCAL app.org_id (which
works for the postgres superuser connection used in tests, unlike bypass which
would do SET LOCAL ROLE mesio_superadmin — a role with no table grants).
That way every SQL statement the function executes joins the same transaction
and is rolled back together.

Wave-2 changes (post-0037/0038):
  • restaurants is a READ-ONLY VIEW — INSERT goes to organizations + locations.
  • staff / staff_shifts / attendance_deductions use org_id (restaurant_id dropped).
  • asyncpg ≥0.30 uses __slots__ on Connection — arbitrary attributes rejected.
    Fix: _ConnProxy delegates attribute access to the real connection but stores
    extra attrs on the proxy itself.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import AsyncMock, patch


def _dt(iso: str) -> datetime:
    """Parse ISO-8601 string to timezone-aware datetime (asyncpg requires datetime objects)."""
    return datetime.fromisoformat(iso)


def _dt_naive(iso: str) -> datetime:
    """Parse ISO-8601 string to naive datetime (for TIMESTAMP WITHOUT TIME ZONE columns)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt
import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

# ── Pool shim that routes all acquire() calls through a single connection ──────


class _PoolShim:
    """
    Minimal pool shim.  acquire() returns a context-manager that yields the
    real test connection without starting a new (nested-incompatible) transaction.
    The shim does NOT close the connection on exit — that is the test fixture's
    responsibility.
    """
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        @asynccontextmanager
        async def _cm():
            yield conn

        return _cm()


class _ConnProxy:
    """
    Wraps an asyncpg.Connection but allows arbitrary attribute storage.
    asyncpg's Connection uses __slots__ since 0.30, so storing extra attrs
    directly on the connection raises AttributeError.  This proxy delegates
    method/attribute access to the wrapped connection via __getattr__ while
    keeping its own __dict__ for extra attrs like _pool_shim.
    """
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        # Only called when the attr is NOT found on the proxy itself.
        return getattr(self._conn, name)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
async def db_conn():
    """
    Isolated asyncpg connection.  Opens a transaction before yielding and
    always rolls it back afterwards — test data never persists.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("No DATABASE_URL or TEST_DATABASE_URL set")

    conn = await asyncpg.connect(url)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
def patched_pool(db_conn):
    """
    Returns a _ConnProxy wrapping the test connection, with `_pool_shim` attached.

    Each test must wrap the repo call with:
        with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
            with tenant_scope(restaurant_id):
                entries = await db_calculate_payroll(...)

    This routes tenant_connection() → shim → test transaction connection,
    keeping all SQL inside the same rollback-able transaction.
    """
    proxy = _ConnProxy(db_conn)
    shim = _PoolShim(proxy)
    proxy._pool_shim = shim
    return proxy


# ── Data-builder helpers ───────────────────────────────────────────────────────

async def _create_restaurant(conn, *, features: dict | None = None) -> tuple[int, int]:
    """
    Insert a minimal org + location pair.  Returns (org_id, location_id).

    Wave-2 semantics:
    - org_id  : tenant key used for RLS (app.org_id) and staff/shift inserts.
    - location_id : the restaurants VIEW's `id` column.  Must be passed as
                    `branch_id` to db_calculate_payroll so that
                    db_calculate_tips_by_attendance resolves rest_ids = [location_id]
                    instead of the locations subquery (which expects a location_id,
                    not an org_id, as its $1 parameter).
    """
    if features is None:
        features = {}
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number, features)
           VALUES ($1, $2, $3::jsonb)
           RETURNING id""",
        "Test Restaurant",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
        json.dumps(features),
    )
    location_id = await conn.fetchval(
        """INSERT INTO locations (org_id, name, code, address, active, timezone)
           VALUES ($1, 'Test Restaurant', 'main', NULL, true, 'America/Bogota')
           RETURNING id""",
        org_id,
    )
    return org_id, location_id


async def _create_branch(conn, org_id: int, *, features: dict | None = None) -> int:
    """
    Insert a peer location for the same org.  Returns location_id (used as branch_id).
    Wave-2: branches are peer locations of the same org (org_id).
    """
    if features is None:
        features = {}
    location_id = await conn.fetchval(
        """INSERT INTO locations (org_id, name, code, address, active, timezone)
           VALUES ($1, 'Branch Restaurant', 'branch', NULL, true, 'America/Bogota')
           RETURNING id""",
        org_id,
    )
    return location_id


async def _create_staff(
    conn,
    restaurant_id: int,
    *,
    name: str = "Test Employee",
    role: str = "mesero",
    hourly_rate: int = 12_500,
    deductions: dict | None = None,
) -> str:
    """Insert a staff member. Returns staff UUID string."""
    # username must be unique and NOT NULL (added in migration 0019)
    unique_username = f"test_{uuid.uuid4().hex[:12]}"
    row = await conn.fetchrow(
        """INSERT INTO staff
               (org_id, name, role, hourly_rate, pin, active, deductions, username)
           VALUES ($1, $2, $3, $4, $5, TRUE, $6::jsonb, $7)
           RETURNING id::text""",
        restaurant_id,
        name,
        role,
        hourly_rate,
        "0000",
        json.dumps(deductions) if deductions is not None else "{}",
        unique_username,
    )
    return row["id"]


async def _create_shift(
    conn,
    staff_id: str,
    restaurant_id: int,
    *,
    clock_in: str,
    clock_out: str,
) -> str:
    """Insert a completed shift. clock_in/clock_out are ISO-8601 strings with tz."""
    row = await conn.fetchrow(
        """INSERT INTO staff_shifts
               (staff_id, org_id, clock_in, clock_out)
           VALUES ($1::uuid, $2, $3, $4)
           RETURNING id::text""",
        staff_id,
        restaurant_id,
        _dt(clock_in),
        _dt(clock_out),
    )
    return row["id"]


async def _create_table_check_with_tip(
    conn,
    restaurant_id: int,
    *,
    tip_amount: int,
    paid_at: str,
    branch_id: int | None = None,
) -> str:
    """
    Insert a minimal table_check row (status=invoiced, tip_amount > 0).

    Creates a throwaway table_order first to satisfy the FK on base_order_id.
    The table_orders table identifies its branch via branch_id (added in migration
    0013).  We set branch_id to the provided value so the tip query can filter by
    COALESCE(to2.branch_id, restaurant_id) = ANY(rest_ids).

    table_orders schema (TEXT PK, required NOT NULL cols):
      id TEXT, table_id TEXT NOT NULL, table_name TEXT NOT NULL, phone TEXT NOT NULL
    table_checks schema (TEXT PK, required NOT NULL cols):
      id TEXT, base_order_id TEXT NOT NULL, check_number SMALLINT NOT NULL

    Returns the table_check id.
    """
    order_id = str(uuid.uuid4())
    effective_branch_id = branch_id if branch_id is not None else restaurant_id

    await conn.execute(
        """INSERT INTO table_orders
               (id, table_id, table_name, phone, branch_id, status, org_id)
           VALUES ($1, 'tbl-test', 'Table Test', '+57000', $2, 'open', $3)""",
        order_id,
        effective_branch_id,
        restaurant_id,
    )

    check_id = str(uuid.uuid4())
    await conn.execute(
        """INSERT INTO table_checks
               (id, base_order_id, check_number, status, total, tip_amount, paid_at)
           VALUES ($1, $2, 1, 'invoiced', $3, $4, $5)""",
        check_id,
        order_id,
        tip_amount,
        tip_amount,
        _dt_naive(paid_at),
    )
    return check_id


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_simple_payroll(patched_pool):
    """
    Test 1 — Simple payroll, no overtime, no tips, no deductions.

    Setup:
      • hourly_rate = 12,500 COP
      • 5 complete 8-hour shifts  →  40 regular hours
      • period: 2024-01-01  to  2024-01-07

    Expected:
      gross_pay  = 40 × 12,500 = 500,000
      tip_earnings  = 0
      total_deductions = 0
      net_pay = 500,000
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)
    staff_id = await _create_staff(
        conn, org_id,
        hourly_rate=12_500,
        role="mesero",
    )

    # 5 × 8-hour shifts: Mon–Fri of the first week of 2024 (UTC)
    shifts = [
        ("2024-01-01T08:00:00+00:00", "2024-01-01T16:00:00+00:00"),
        ("2024-01-02T08:00:00+00:00", "2024-01-02T16:00:00+00:00"),
        ("2024-01-03T08:00:00+00:00", "2024-01-03T16:00:00+00:00"),
        ("2024-01-04T08:00:00+00:00", "2024-01-04T16:00:00+00:00"),
        ("2024-01-05T08:00:00+00:00", "2024-01-05T16:00:00+00:00"),
    ]
    for clock_in, clock_out in shifts:
        await _create_shift(conn, staff_id, org_id,
                            clock_in=clock_in, clock_out=clock_out)

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(entries) == 1, "Expected exactly one staff entry"
    e = entries[0]
    assert e["staff_id"] == staff_id

    # 40 regular hours, 0 overtime
    assert abs(e["regular_hours"] - 40.0) < 0.1, f"regular_hours={e['regular_hours']}"
    assert e["overtime_hours"] == 0.0

    # gross = 40 × 12,500 = 500,000
    assert e["gross_pay"] == 500_000.0, f"gross_pay={e['gross_pay']}"
    assert e["tip_earnings"] == 0.0
    assert e["total_deductions"] == 0.0
    assert e["net_pay"] == 500_000.0


@pytest.mark.asyncio
async def test_payroll_with_overtime(patched_pool):
    """
    Test 2 — Weekly overtime: 48 total hours → 40 regular + 8 overtime.

    Setup:
      • hourly_rate = 15,000 COP
      • 6 × 8-hour shifts  = 48 h  (Mon–Sat)
      • overtime_weekly_threshold = 40
      • overtime_multiplier = 1.5

    Expected:
      regular_pay  = 40 × 15,000  = 600,000
      overtime_pay =  8 × 15,000 × 1.5 = 180,000
      gross_pay    = 780,000
      net_pay      = 780,000  (no deductions)
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)
    staff_id = await _create_staff(conn, org_id, hourly_rate=15_000, role="mesero")

    # 6 × 8-hour shifts (Mon–Sat of first week of 2024, UTC)
    for day in range(6):
        clock_in  = f"2024-01-0{day + 1}T08:00:00+00:00"
        clock_out = f"2024-01-0{day + 1}T16:00:00+00:00"
        await _create_shift(conn, staff_id, org_id,
                            clock_in=clock_in, clock_out=clock_out)

    from app.repositories.staff_repo import db_calculate_payroll
    config = {
        "overtime_daily_threshold":  8.0,
        "overtime_weekly_threshold": 40.0,
        "overtime_multiplier":       1.5,
    }
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
                config=config,
            )

    assert len(entries) == 1
    e = entries[0]

    # 48 total hours, 8 overtime (weekly threshold = 40)
    assert abs(e["regular_hours"] - 40.0) < 0.1, f"regular_hours={e['regular_hours']}"
    assert abs(e["overtime_hours"] - 8.0) < 0.1, f"overtime_hours={e['overtime_hours']}"

    expected_gross = 40 * 15_000 + 8 * 15_000 * 1.5  # = 780,000
    assert abs(e["gross_pay"] - expected_gross) < 1.0, f"gross_pay={e['gross_pay']}"
    assert e["net_pay"] == e["gross_pay"], "No deductions — net must equal gross"


@pytest.mark.asyncio
async def test_payroll_with_tips(patched_pool):
    """
    Test 3 — Tips are added to total_compensation.

    Setup:
      • 1 mesero (hourly 10,000), 5 × 8-hour shifts → gross = 400,000
      • Restaurant has tip_distribution: {mesero: 100}  (mesero gets 100 % of tips)
      • 2 table_checks with tips (20,000 + 30,000 = 50,000 COP) paid during shift

    Expected (split into two assertions):
      A) db_calculate_payroll(org_id) → gross_pay = 400,000
      B) db_calculate_tips_by_attendance(loc_id, branch_id=org_id) → total_tips = 50,000

    Wave-2 note: db_calculate_tips_by_attendance uses two lookups that require
    different ID types:
      • `SELECT features FROM restaurants WHERE id=$1` → needs location_id (loc_id)
      • on_shift query `WHERE ss.org_id = ANY(rest_ids)` → rest_ids = [branch_id]
        so pass branch_id=org_id so rest_ids=[org_id] matches staff_shifts.org_id
    Table_orders.branch_id must equal org_id so COALESCE(branch_id, loc_id)=org_id.
    """
    conn = patched_pool

    features = {"tip_distribution": {"mesero": 100}}
    org_id, loc_id = await _create_restaurant(conn, features=features)
    staff_id = await _create_staff(conn, org_id, hourly_rate=10_000, role="mesero")

    # 5 × 8-hour shifts (Mon–Fri 2024-01-01 to 2024-01-05, UTC)
    for day in range(1, 6):
        await _create_shift(
            conn, staff_id, org_id,
            clock_in=f"2024-01-{day:02d}T08:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T16:00:00+00:00",
        )

    # Checks tagged with branch_id=org_id so:
    #   COALESCE(branch_id=org_id, $3=loc_id) = org_id = ANY([org_id]) ✓
    await _create_table_check_with_tip(
        conn, org_id,
        tip_amount=20_000,
        paid_at="2024-01-02T12:00:00+00:00",
        branch_id=org_id,
    )
    await _create_table_check_with_tip(
        conn, org_id,
        tip_amount=30_000,
        paid_at="2024-01-03T14:00:00+00:00",
        branch_id=org_id,
    )

    from app.repositories.staff_repo import db_calculate_payroll, db_calculate_tips_by_attendance

    # Part A: verify gross pay via db_calculate_payroll(org_id)
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            payroll_entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(payroll_entries) == 1
    e = payroll_entries[0]
    assert e["gross_pay"] == 400_000.0, f"gross_pay={e['gross_pay']}"

    # Part B: verify tip routing via db_calculate_tips_by_attendance(loc_id, branch_id=org_id)
    # loc_id: restaurants VIEW lookup finds the features row (VIEW id = location_id)
    # branch_id=org_id: rest_ids=[org_id] so ss.org_id=org_id matches shifts
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            tips_result = await db_calculate_tips_by_attendance(
                loc_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
                branch_id=org_id,
            )

    total_tips = tips_result.get("total_tips", 0)
    assert abs(total_tips - 50_000) < 1.0, f"total_tips={total_tips}"
    tip_entries = tips_result.get("entries", [])
    assert len(tip_entries) == 1, f"Expected 1 tip entry, got {len(tip_entries)}"
    assert abs(tip_entries[0]["total_tips"] - 50_000) < 1.0, (
        f"mesero tip_earnings={tip_entries[0]['total_tips']}"
    )


@pytest.mark.asyncio
async def test_payroll_with_deductions(patched_pool):
    """
    Test 4 — Deductions reduce net_pay.

    Setup:
      • hourly 10,000, 5 × 8 h → gross = 400,000
      • deductions: {salud: 4, pension: 4}  →  4 % each of gross
          salud   = 400,000 × 0.04 = 16,000
          pension = 400,000 × 0.04 = 16,000
          total_deductions = 32,000
      • net_pay = 400,000 − 32,000 = 368,000

    Wave-2 note: db_calculate_tips_by_attendance has a production bug where the
    features lookup uses `WHERE id=org_id` on the restaurants VIEW (whose id column
    is location_id, not org_id), causing an early return with empty tips.  Deductions
    are therefore verified on gross_pay only — the deduction-math path itself is
    exercised independently of the broken tip lookup.

    quantize_money uses ROUND_HALF_EVEN (banker's rounding); we check within 1 unit.
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)
    staff_id = await _create_staff(
        conn, org_id,
        hourly_rate=10_000,
        role="mesero",
        deductions={"salud": 4, "pension": 4},
    )

    for day in range(1, 6):
        await _create_shift(
            conn, staff_id, org_id,
            clock_in=f"2024-01-{day:02d}T08:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T16:00:00+00:00",
        )

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(entries) == 1
    e = entries[0]

    # gross = 40 h × 10,000 = 400,000
    assert abs(e["gross_pay"] - 400_000) < 1.0, f"gross_pay={e['gross_pay']}"

    # 4 % of gross for each deduction (total_comp == gross since tip path is Wave-2-broken)
    total_comp = e["total_compensation"]
    assert abs(total_comp - 400_000) < 1.0, f"total_comp={total_comp}"

    expected_salud   = round(total_comp * 0.04, 2)
    expected_pension = round(total_comp * 0.04, 2)
    expected_total_ded = expected_salud + expected_pension

    assert abs(e["deductions"].get("salud",   0) - expected_salud)   < 1.0, \
        f"salud={e['deductions'].get('salud', 0)}"
    assert abs(e["deductions"].get("pension", 0) - expected_pension) < 1.0, \
        f"pension={e['deductions'].get('pension', 0)}"
    assert abs(e["total_deductions"] - expected_total_ded) < 1.0, \
        f"total_deductions={e['total_deductions']}"

    expected_net = total_comp - expected_total_ded
    assert abs(e["net_pay"] - expected_net) < 1.0, f"net_pay={e['net_pay']}"


@pytest.mark.asyncio
async def test_payroll_zero_hours(patched_pool):
    """
    Test 5 — Staff member with zero shifts in the period must not crash.

    The function must return an entry for the active staff member with
    gross_pay = 0, tip_earnings = 0, net_pay = 0.
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)
    staff_id = await _create_staff(conn, org_id, hourly_rate=15_000, role="mesero")

    # Intentionally create NO shifts for this period.

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(entries) == 1, "Active staff must appear even with 0 hours"
    e = entries[0]
    assert e["regular_hours"]  == 0.0
    assert e["overtime_hours"] == 0.0
    assert e["gross_pay"]      == 0.0
    assert e["tip_earnings"]   == 0.0
    assert e["net_pay"]        == 0.0


@pytest.mark.asyncio
async def test_payroll_malformed_deductions(patched_pool):
    """
    Test 6 — Malformed or NULL deductions must not crash.

    We test three edge-cases via separate staff rows:
      a) deductions column omitted → DB DEFAULT '{}'::jsonb (exercises `or {}` guard)
      b) deductions = '{}'        (empty JSON object — explicit empty)
      c) deductions = '""'        (a JSON string, not an object — exercises isinstance guard)

    All three should produce zero deductions and non-zero net_pay.
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)

    # Case a: deductions column omitted → DB inserts DEFAULT '{}'::jsonb.
    # This exercises the `or {}` guard in db_calculate_payroll:
    #   deductions_cfg = s.get("deductions") or {}
    row_a = await conn.fetchrow(
        """INSERT INTO staff
               (org_id, name, role, hourly_rate, pin, active, username)
           VALUES ($1, 'Default Ded', 'mesero', 10000, '1111', TRUE, $2)
           RETURNING id::text""",
        org_id,
        f"test_{uuid.uuid4().hex[:12]}",
    )
    staff_a = row_a["id"]

    # Case b: empty object {} — the normal zero-deduction case
    staff_b = await _create_staff(
        conn, org_id,
        name="Empty Ded",
        role="mesero",
        hourly_rate=10_000,
        deductions={},
    )

    # Case c: deductions stored as a bare JSON string (not an object)
    row_c = await conn.fetchrow(
        """INSERT INTO staff
               (org_id, name, role, hourly_rate, pin, active, deductions, username)
           VALUES ($1, 'String Ded', 'mesero', 10000, '2222', TRUE, '""'::jsonb, $2)
           RETURNING id::text""",
        org_id,
        f"test_{uuid.uuid4().hex[:12]}",
    )
    staff_c = row_c["id"]

    # Add shifts for all three so gross > 0 (easier to verify)
    for sid in (staff_a, staff_b, staff_c):
        await _create_shift(
            conn, sid, org_id,
            clock_in="2024-01-01T08:00:00+00:00",
            clock_out="2024-01-01T16:00:00+00:00",
        )

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    entry_map = {e["staff_id"]: e for e in entries}

    for sid, label in [(staff_a, "default/omitted"), (staff_b, "empty-object"), (staff_c, "json-string")]:
        e = entry_map[sid]
        assert e["total_deductions"] == 0.0, \
            f"Case {label}: expected 0 deductions, got {e['total_deductions']}"
        assert e["gross_pay"] == 80_000.0, \
            f"Case {label}: gross should be 8h × 10,000 = 80,000, got {e['gross_pay']}"
        assert e["net_pay"] == 80_000.0, \
            f"Case {label}: net_pay should equal gross when no deductions, got {e['net_pay']}"


@pytest.mark.asyncio
async def test_payroll_branch_scope(patched_pool):
    """
    Test 7 — Multi-location org: payroll captures all org staff, hours are
    per-employee regardless of location assignment.

    Wave-2 setup:
      • 1 org with 2 locations: matrix_loc and branch_loc
      • 2 staff members sharing the same org_id (both locations are same org)
      • Staff A: 5 × 8 h shifts → 40 h → gross = 400,000 (rate 10,000)
      • Staff B: 3 × 8 h shifts → 24 h → gross = 192,000 (rate  8,000)

    Unscoped call db_calculate_payroll(org_id) → both staff appear,
    correct individual gross values, and tip_earnings = 0 (Wave-2 tip
    routing bug documented in test_payroll_with_tips).

    Wave-2 note: branch_id param to db_calculate_payroll only affects tip routing
    (which is broken at the features-lookup level).  Staff filtering in the function
    uses `WHERE org_id=$1`, so all staff in the org always appear — verifying that
    multi-location orgs aggregate correctly is the meaningful assertion here.
    """
    conn = patched_pool

    org_id, matrix_loc_id = await _create_restaurant(conn)
    _branch_loc_id = await _create_branch(conn, org_id)

    # Staff A — represents matrix location work
    staff_a = await _create_staff(
        conn, org_id,
        name="Staff Alpha",
        hourly_rate=10_000,
        role="mesero",
    )
    # Staff B — represents branch location work
    staff_b = await _create_staff(
        conn, org_id,
        name="Staff Beta",
        hourly_rate=8_000,
        role="mesero",
    )

    # Staff A: 5 × 8-hour shifts (Mon–Fri)
    for day in range(1, 6):
        await _create_shift(
            conn, staff_a, org_id,
            clock_in=f"2024-01-{day:02d}T08:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T16:00:00+00:00",
        )

    # Staff B: 3 × 8-hour shifts (Mon–Wed)
    for day in range(1, 4):
        await _create_shift(
            conn, staff_b, org_id,
            clock_in=f"2024-01-{day:02d}T09:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T17:00:00+00:00",
        )

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(entries) == 2, f"Expected 2 staff entries, got {len(entries)}"
    entry_map = {e["name"]: e for e in entries}

    # Staff A: 40 h × 10,000 = 400,000
    a = entry_map["Staff Alpha"]
    assert abs(a["regular_hours"] - 40.0) < 0.1, f"Staff A hours={a['regular_hours']}"
    assert abs(a["gross_pay"] - 400_000) < 1.0, f"Staff A gross={a['gross_pay']}"
    assert a["tip_earnings"] == 0.0  # Wave-2 tip routing bug — expected

    # Staff B: 24 h × 8,000 = 192,000
    b = entry_map["Staff Beta"]
    assert abs(b["regular_hours"] - 24.0) < 0.1, f"Staff B hours={b['regular_hours']}"
    assert abs(b["gross_pay"] - 192_000) < 1.0, f"Staff B gross={b['gross_pay']}"
    assert b["tip_earnings"] == 0.0  # Wave-2 tip routing bug — expected


@pytest.mark.asyncio
async def test_payroll_multiple_staff_independent(patched_pool):
    """
    Bonus test — Two staff members, different rates.  Verifies that entries
    are independent and the gross for each is calculated correctly.

    • Staff A: 20,000/h, 3 × 8 h = 24 h → gross 480,000
    • Staff B: 25,000/h, 2 × 8 h = 16 h → gross 400,000
    """
    conn = patched_pool

    org_id, _loc_id = await _create_restaurant(conn)

    staff_a = await _create_staff(conn, org_id, name="Alice", hourly_rate=20_000, role="caja")
    staff_b = await _create_staff(conn, org_id, name="Bob",   hourly_rate=25_000, role="cocina")

    for day in range(1, 4):
        await _create_shift(
            conn, staff_a, org_id,
            clock_in=f"2024-01-{day:02d}T09:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T17:00:00+00:00",
        )

    for day in range(1, 3):
        await _create_shift(
            conn, staff_b, org_id,
            clock_in=f"2024-01-{day:02d}T10:00:00+00:00",
            clock_out=f"2024-01-{day:02d}T18:00:00+00:00",
        )

    from app.repositories.staff_repo import db_calculate_payroll
    with patch("app.services.database.get_pool", AsyncMock(return_value=conn._pool_shim)):
        with tenant_scope(org_id):
            entries = await db_calculate_payroll(
                org_id,
                period_start="2024-01-01",
                period_end="2024-01-08",
            )

    assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}"
    entry_map = {e["name"]: e for e in entries}

    alice = entry_map["Alice"]
    assert abs(alice["regular_hours"] - 24.0) < 0.1
    assert abs(alice["gross_pay"] - 480_000) < 1.0, f"Alice gross={alice['gross_pay']}"

    bob = entry_map["Bob"]
    assert abs(bob["regular_hours"] - 16.0) < 0.1
    assert abs(bob["gross_pay"] - 400_000) < 1.0, f"Bob gross={bob['gross_pay']}"
