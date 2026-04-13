"""
Integration tests for db_calculate_tips_by_attendance.

Uses a REAL PostgreSQL connection (TEST_DATABASE_URL or DATABASE_URL).
Each test runs inside a transaction that is always rolled back, so the
database is left in a pristine state even after failures.

Run:
    pytest tests/test_tips.py -v
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal


def _dt(iso: str) -> datetime:
    """Parse ISO-8601 string to timezone-aware datetime (asyncpg requires datetime objects)."""
    return datetime.fromisoformat(iso)


def _dt_naive(iso: str) -> datetime:
    """Parse ISO-8601 string to naive datetime (for TIMESTAMP WITHOUT TIME ZONE columns)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt
from unittest.mock import AsyncMock, patch

import pytest

from app.repositories.staff_repo import db_calculate_tips_by_attendance


# ── Pool shim that routes pool.acquire() to the test connection ───────────────
#
# db_calculate_tips_by_attendance calls:
#     pool = await _get_pool()
#     async with pool.acquire() as conn:
#         ...
#
# We wrap the already-open test connection in a minimal async context manager
# so the function never opens a second connection (and therefore stays inside
# the same open transaction that conftest.db_conn wraps around every test).

def _make_pool_for_conn(conn):
    """Return a fake pool whose .acquire() yields `conn` without touching it."""

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = AsyncMock()
    pool.acquire = _acquire
    return pool


# ── Tiny helper IDs ──────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


# ── Fixtures for inserting test data ─────────────────────────────────────────


async def _insert_restaurant(conn, *, tip_distribution: dict) -> int:
    """Insert a minimal restaurant and return its id."""
    row = await conn.fetchrow(
        """
        INSERT INTO restaurants (name, whatsapp_number, address, features)
        VALUES ($1, $2, '', $3::jsonb)
        RETURNING id
        """,
        "Test Restaurant",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",  # unique phone
        json.dumps({"tip_distribution": tip_distribution}),
    )
    return row["id"]


async def _insert_staff(conn, *, restaurant_id: int, name: str, role: str) -> str:
    """Insert a staff member and return their UUID as a string."""
    # username must be unique and NOT NULL (added in migration 0019)
    unique_username = f"test_{uuid.uuid4().hex[:12]}"
    row = await conn.fetchrow(
        """
        INSERT INTO staff (id, restaurant_id, name, role, pin, username)
        VALUES ($1, $2, $3, $4, '', $5)
        RETURNING id
        """,
        uuid.uuid4(),
        restaurant_id,
        name,
        role,
        unique_username,
    )
    return str(row["id"])


async def _insert_shift(
    conn,
    *,
    staff_id: str,
    restaurant_id: int,
    clock_in: str,
    clock_out: str | None,
) -> None:
    """Insert a staff_shift row."""
    await conn.execute(
        """
        INSERT INTO staff_shifts (id, staff_id, restaurant_id, clock_in, clock_out)
        VALUES ($1, $2::uuid, $3, $4, $5)
        """,
        uuid.uuid4(),
        staff_id,
        restaurant_id,
        _dt(clock_in),
        _dt(clock_out) if clock_out else None,
    )


async def _insert_table_order(conn, *, restaurant_id: int) -> str:
    """Insert a table_order and return its base_order_id."""
    base_id = _uid()
    await conn.execute(
        """
        INSERT INTO table_orders (id, table_id, table_name, phone, base_order_id, branch_id)
        VALUES ($1, 'T1', 'Mesa 1', '+57300', $1, $2)
        """,
        base_id,
        restaurant_id,
    )
    return base_id


async def _insert_check(
    conn,
    *,
    base_order_id: str,
    tip_amount: int,
    paid_at: str,
    status: str = "invoiced",
) -> str:
    """Insert a table_check and return its id."""
    check_id = _uid()
    await conn.execute(
        """
        INSERT INTO table_checks
            (id, base_order_id, check_number, tip_amount, status, paid_at,
             subtotal, tax_amount, total, items, payments)
        VALUES
            ($1, $2, 1, $3, $4, $5,
             0, 0, $3, '[]'::jsonb, '[]'::jsonb)
        """,
        check_id,
        base_order_id,
        tip_amount,
        status,
        _dt_naive(paid_at),
    )
    return check_id


# ── Shared period ──────────────────────────────────────────────────────────

PERIOD_START = "2024-01-01T00:00:00+00:00"
PERIOD_END   = "2024-01-02T00:00:00+00:00"
PAID_AT_14   = "2024-01-01T14:00:00+00:00"


# ── Test 1: Basic even distribution ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_basic_distribution(db_conn):
    """
    Config: mesero=60 %, cocina=40 %
    1 check  tip=100 000
    2 meseros on shift 12–20, 1 cocinero on shift 10–18

    Expected:
      mesero1 = 30 000
      mesero2 = 30 000
      cocinero = 40 000
      unallocated = 0
    """
    rest_id = await _insert_restaurant(
        db_conn, tip_distribution={"mesero": 60, "cocina": 40}
    )

    m1 = await _insert_staff(db_conn, restaurant_id=rest_id, name="Mesero 1", role="mesero")
    m2 = await _insert_staff(db_conn, restaurant_id=rest_id, name="Mesero 2", role="mesero")
    c1 = await _insert_staff(db_conn, restaurant_id=rest_id, name="Cocinero 1", role="cocina")

    for sid in [m1, m2]:
        await _insert_shift(
            db_conn,
            staff_id=sid,
            restaurant_id=rest_id,
            clock_in="2024-01-01T12:00:00+00:00",
            clock_out="2024-01-01T20:00:00+00:00",
        )
    await _insert_shift(
        db_conn,
        staff_id=c1,
        restaurant_id=rest_id,
        clock_in="2024-01-01T10:00:00+00:00",
        clock_out="2024-01-01T18:00:00+00:00",
    )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(db_conn, base_order_id=base_id, tip_amount=100_000, paid_at=PAID_AT_14)

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["total_tips"] == 100_000
    assert result["unallocated"] == 0

    by_name = {e["name"]: e["total_tips"] for e in result["entries"]}
    assert by_name["Mesero 1"] == 30_000
    assert by_name["Mesero 2"] == 30_000
    assert by_name["Cocinero 1"] == 40_000


# ── Test 2: Non-divisible tip — rounding test ────────────────────────────────

@pytest.mark.asyncio
async def test_rounding_three_meseros(db_conn):
    """
    Config: mesero=100 %
    1 check  tip=10 000
    3 meseros on shift

    Each gets ≈ 3 333.  The sum of quantized values must be within 1 COP
    of 10 000 (rounding cannot lose or create money beyond 1 unit).
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    meseros = []
    for i in range(3):
        sid = await _insert_staff(
            db_conn, restaurant_id=rest_id, name=f"Mesero {i+1}", role="mesero"
        )
        meseros.append(sid)
        await _insert_shift(
            db_conn,
            staff_id=sid,
            restaurant_id=rest_id,
            clock_in="2024-01-01T10:00:00+00:00",
            clock_out="2024-01-01T22:00:00+00:00",
        )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(db_conn, base_order_id=base_id, tip_amount=10_000, paid_at=PAID_AT_14)

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["unallocated"] == 0
    total_allocated = sum(e["total_tips"] for e in result["entries"])
    assert abs(total_allocated - 10_000) <= 1, (
        f"total_allocated={total_allocated} is too far from 10 000"
    )
    # Each mesero should get approximately 3 333
    for entry in result["entries"]:
        assert 3_332 <= entry["total_tips"] <= 3_334, (
            f"{entry['name']} got {entry['total_tips']}, expected ~3 333"
        )


# ── Test 3: No staff on shift → unallocated ──────────────────────────────────

@pytest.mark.asyncio
async def test_no_staff_on_shift_unallocated(db_conn):
    """
    Config: mesero=100 %
    1 check  tip=50 000, paid_at=14:00
    Staff shift is OUTSIDE the paid_at time (08:00–10:00)

    Expected: unallocated=50 000
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    sid = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Off", role="mesero"
    )
    # Shift is 08:00–10:00 — does NOT cover 14:00
    await _insert_shift(
        db_conn,
        staff_id=sid,
        restaurant_id=rest_id,
        clock_in="2024-01-01T08:00:00+00:00",
        clock_out="2024-01-01T10:00:00+00:00",
    )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(db_conn, base_order_id=base_id, tip_amount=50_000, paid_at=PAID_AT_14)

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["total_tips"] == 50_000
    assert result["unallocated"] == 50_000
    assert result["entries"] == []


# ── Test 4: Role not in config → ignored ────────────────────────────────────

@pytest.mark.asyncio
async def test_role_not_in_config_ignored(db_conn):
    """
    Config: mesero=100 % (cocina is NOT in config)
    1 mesero + 1 cocinero both on shift at 14:00
    Check tip=20 000

    Expected: mesero gets 20 000, cocinero gets 0, unallocated=0
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    mesero = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Principal", role="mesero"
    )
    cocinero = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Cocinero Extra", role="cocina"
    )

    for sid in [mesero, cocinero]:
        await _insert_shift(
            db_conn,
            staff_id=sid,
            restaurant_id=rest_id,
            clock_in="2024-01-01T10:00:00+00:00",
            clock_out="2024-01-01T22:00:00+00:00",
        )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(db_conn, base_order_id=base_id, tip_amount=20_000, paid_at=PAID_AT_14)

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["total_tips"] == 20_000
    assert result["unallocated"] == 0

    by_name = {e["name"]: e["total_tips"] for e in result["entries"]}
    assert by_name["Mesero Principal"] == 20_000
    # Cocinero Extra must NOT appear (role not in config → filtered by the SQL query)
    assert "Cocinero Extra" not in by_name


# ── Test 5: Multiple checks in period — different staff at different times ────

@pytest.mark.asyncio
async def test_multiple_checks_accumulate(db_conn):
    """
    Config: mesero=100 %

    Check A  tip=30 000  paid_at=10:00  → only mesero_morning on shift (08–12)
    Check B  tip=50 000  paid_at=15:00  → only mesero_afternoon on shift (13–21)
    Check C  tip=20 000  paid_at=10:30  → only mesero_morning on shift

    Expected:
      mesero_morning   = 30 000 + 20 000 = 50 000
      mesero_afternoon = 50 000
      unallocated      = 0
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    morning = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Mañana", role="mesero"
    )
    afternoon = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Tarde", role="mesero"
    )

    await _insert_shift(
        db_conn,
        staff_id=morning,
        restaurant_id=rest_id,
        clock_in="2024-01-01T08:00:00+00:00",
        clock_out="2024-01-01T12:00:00+00:00",
    )
    await _insert_shift(
        db_conn,
        staff_id=afternoon,
        restaurant_id=rest_id,
        clock_in="2024-01-01T13:00:00+00:00",
        clock_out="2024-01-01T21:00:00+00:00",
    )

    base_a = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(
        db_conn,
        base_order_id=base_a,
        tip_amount=30_000,
        paid_at="2024-01-01T10:00:00+00:00",
    )

    base_b = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(
        db_conn,
        base_order_id=base_b,
        tip_amount=50_000,
        paid_at="2024-01-01T15:00:00+00:00",
    )

    # Reuse base_a for a second check: check_number must differ
    check_c_id = _uid()
    await db_conn.execute(
        """
        INSERT INTO table_checks
            (id, base_order_id, check_number, tip_amount, status, paid_at,
             subtotal, tax_amount, total, items, payments)
        VALUES ($1, $2, 2, 20000, 'invoiced', '2024-01-01T10:30:00+00:00'::timestamptz,
                0, 0, 20000, '[]'::jsonb, '[]'::jsonb)
        """,
        check_c_id,
        base_a,
    )

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["total_tips"] == 100_000
    assert result["unallocated"] == 0

    by_name = {e["name"]: e["total_tips"] for e in result["entries"]}
    assert by_name["Mesero Mañana"] == 50_000
    assert by_name["Mesero Tarde"] == 50_000

    # Ticket count verification
    by_name_count = {e["name"]: e["tickets_contributed"] for e in result["entries"]}
    assert by_name_count["Mesero Mañana"] == 2   # Check A + Check C
    assert by_name_count["Mesero Tarde"] == 1    # Check B only


# ── Test 6: Empty tip_distribution config → early return ─────────────────────

@pytest.mark.asyncio
async def test_empty_tip_distribution_config(db_conn):
    """
    Config: {} (empty)

    The function must return immediately with empty entries and 0 totals.
    No checks or staff are needed.
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={})

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["entries"] == []
    assert result["total_tips"] == 0
    assert result["unallocated"] == 0
    assert result["pct_config"] == {}


# ── Test 7: Check with status != 'invoiced' → ignored ────────────────────────

@pytest.mark.asyncio
async def test_non_invoiced_check_ignored(db_conn):
    """
    A check with status='open' (not 'invoiced') must NOT be counted even if
    tip_amount > 0.  The SQL WHERE clause enforces this.
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    sid = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Activo", role="mesero"
    )
    await _insert_shift(
        db_conn,
        staff_id=sid,
        restaurant_id=rest_id,
        clock_in="2024-01-01T10:00:00+00:00",
        clock_out="2024-01-01T22:00:00+00:00",
    )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    # status='open' — should be ignored
    await _insert_check(
        db_conn,
        base_order_id=base_id,
        tip_amount=40_000,
        paid_at=PAID_AT_14,
        status="open",
    )

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["entries"] == []
    assert result["total_tips"] == 0
    assert result["unallocated"] == 0


# ── Test 8: Check paid outside the period → ignored ──────────────────────────

@pytest.mark.asyncio
async def test_check_outside_period_ignored(db_conn):
    """
    A check paid BEFORE period_start must be excluded.
    """
    rest_id = await _insert_restaurant(db_conn, tip_distribution={"mesero": 100})

    sid = await _insert_staff(
        db_conn, restaurant_id=rest_id, name="Mesero Antiguo", role="mesero"
    )
    await _insert_shift(
        db_conn,
        staff_id=sid,
        restaurant_id=rest_id,
        clock_in="2023-12-31T10:00:00+00:00",
        clock_out="2023-12-31T22:00:00+00:00",
    )

    base_id = await _insert_table_order(db_conn, restaurant_id=rest_id)
    await _insert_check(
        db_conn,
        base_order_id=base_id,
        tip_amount=60_000,
        paid_at="2023-12-31T14:00:00+00:00",  # before PERIOD_START
    )

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            rest_id, PERIOD_START, PERIOD_END
        )

    assert result["entries"] == []
    assert result["total_tips"] == 0
    assert result["unallocated"] == 0


# ── Test 9: Branch scope — branch_id filter ──────────────────────────────────

@pytest.mark.asyncio
async def test_branch_id_filter(db_conn):
    """
    Two branches: branch_a and branch_b under the same matrix.
    Each has a mesero and a check.

    When calling with branch_id=branch_a, only the check for branch_a is
    counted; the mesero from branch_b gets nothing.
    """
    # Insert a minimal parent so branch FK holds
    matrix_id = await _insert_restaurant(
        db_conn, tip_distribution={"mesero": 100}
    )

    # branch_a shares the same tip config
    branch_a_id = await db_conn.fetchval(
        """
        INSERT INTO restaurants (name, whatsapp_number, address, features, parent_restaurant_id)
        VALUES ($1, $2, '', $3::jsonb, $4)
        RETURNING id
        """,
        "Branch A",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
        json.dumps({"tip_distribution": {"mesero": 100}}),
        matrix_id,
    )
    branch_b_id = await db_conn.fetchval(
        """
        INSERT INTO restaurants (name, whatsapp_number, address, features, parent_restaurant_id)
        VALUES ($1, $2, '', $3::jsonb, $4)
        RETURNING id
        """,
        "Branch B",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
        json.dumps({"tip_distribution": {"mesero": 100}}),
        matrix_id,
    )

    # Staff and shifts per branch
    ma = await _insert_staff(
        db_conn, restaurant_id=branch_a_id, name="Mesero Branch A", role="mesero"
    )
    mb = await _insert_staff(
        db_conn, restaurant_id=branch_b_id, name="Mesero Branch B", role="mesero"
    )
    for sid, rid in [(ma, branch_a_id), (mb, branch_b_id)]:
        await _insert_shift(
            db_conn,
            staff_id=sid,
            restaurant_id=rid,
            clock_in="2024-01-01T10:00:00+00:00",
            clock_out="2024-01-01T22:00:00+00:00",
        )

    base_a = await _insert_table_order(db_conn, restaurant_id=branch_a_id)
    await _insert_check(db_conn, base_order_id=base_a, tip_amount=30_000, paid_at=PAID_AT_14)

    base_b = await _insert_table_order(db_conn, restaurant_id=branch_b_id)
    await _insert_check(db_conn, base_order_id=base_b, tip_amount=70_000, paid_at=PAID_AT_14)

    pool = _make_pool_for_conn(db_conn)
    with patch("app.repositories.staff_repo._get_pool", return_value=pool):
        result = await db_calculate_tips_by_attendance(
            matrix_id, PERIOD_START, PERIOD_END, branch_id=branch_a_id
        )

    assert result["total_tips"] == 30_000
    assert result["unallocated"] == 0
    by_name = {e["name"]: e["total_tips"] for e in result["entries"]}
    assert by_name.get("Mesero Branch A") == 30_000
    assert "Mesero Branch B" not in by_name
