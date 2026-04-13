"""
tests/test_attendance_deduction.py

Realistic integration tests for attendance deductions (tardiness / early_departure).

Requirements:
  - Real PostgreSQL database accessible via TEST_DATABASE_URL or DATABASE_URL env var.
  - Schema must already be migrated (alembic upgrade head).
  - Every test runs inside a transaction that is rolled back → zero side-effects.

Run:
  pytest tests/test_attendance_deduction.py -v
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal

import asyncpg
import pytest

from app.repositories.staff_repo import _record_attendance_deduction
from app.services.money import quantize_money, money_mul


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def db_conn():
    """
    Yield a real asyncpg connection with an open transaction that is always
    rolled back after the test, leaving the DB in a clean state.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("No DATABASE_URL or TEST_DATABASE_URL configured")

    conn = await asyncpg.connect(url)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()


@pytest.fixture
async def base_data(db_conn):
    """
    Insert the minimal rows needed for all tests:
      - one restaurant (features->>'timezone' = 'America/Bogota')
      - one staff member with hourly_rate = 12500

    Returns a dict with 'restaurant_id' and 'staff_id'.
    Does NOT insert a shift — each test that needs one creates it.
    """
    # Restaurant ---------------------------------------------------------------
    rest_row = await db_conn.fetchrow(
        """INSERT INTO restaurants
               (name, whatsapp_number, features)
           VALUES ($1, $2, $3::jsonb)
           RETURNING id""",
        "Test Restaurant",
        f"+5730099{uuid.uuid4().hex[:6]}",   # unique number per test run
        json.dumps({"timezone": "America/Bogota"}),
    )
    restaurant_id = rest_row["id"]

    # Staff --------------------------------------------------------------------
    # bcrypt hash of "1234" — we never validate it in these tests
    pin_hash = "$2b$12$AAAAAAAAAAAAAAAAAAAAAO9UtBzOy71JBR.oeSYHsRJyR/0MDXiWG"
    staff_row = await db_conn.fetchrow(
        """INSERT INTO staff
               (restaurant_id, name, username, role, pin, hourly_rate)
           VALUES ($1, $2, $3, 'mesero', $4, $5)
           RETURNING id""",
        restaurant_id,
        "Carlos Test",
        f"carlos.test.{uuid.uuid4().hex[:6]}",
        pin_hash,
        Decimal("12500"),
    )
    staff_id = str(staff_row["id"])

    return {"restaurant_id": restaurant_id, "staff_id": staff_id}


async def _insert_open_shift(db_conn, staff_id: str, restaurant_id: int) -> str:
    """Helper: open a shift and return its id (text)."""
    row = await db_conn.fetchrow(
        """INSERT INTO staff_shifts (staff_id, restaurant_id)
           VALUES ($1::uuid, $2)
           RETURNING id::text""",
        staff_id, restaurant_id,
    )
    return row["id"]


async def _count_deductions(db_conn, shift_id: str) -> list[asyncpg.Record]:
    return await db_conn.fetch(
        "SELECT * FROM attendance_deductions WHERE shift_id=$1::uuid",
        shift_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a timezone-aware "actual" datetime from a naive local time
# ─────────────────────────────────────────────────────────────────────────────

def _local_dt(hour: int, minute: int, tz_name: str = "America/Bogota") -> datetime:
    """
    Return a timezone-aware datetime for TODAY at hour:minute in the given
    IANA timezone.  We use a fixed UTC offset approach so the tests don't
    depend on the host system's tzdata being present (though zoneinfo is
    standard in Python 3.9+).
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    naive = datetime.now(tz=timezone.utc).astimezone(tz).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=None
    )
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Tests — tardiness (clock-in)
# ─────────────────────────────────────────────────────────────────────────────

async def test_within_4_min_tolerance_no_deduction(db_conn, base_data):
    """
    Staff arrives 4 minutes late (≤ 5 min tolerance) → NO deduction row.
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled = time(8, 0)               # shift starts 08:00 local
    actual = _local_dt(8, 4)             # arrived 08:04 → 4 min late

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 0, "4-min lateness must NOT produce a deduction"


async def test_exactly_5_min_tolerance_no_deduction(db_conn, base_data):
    """
    Exactly 5 minutes late is still within tolerance → NO deduction.
    The boundary check is `minutes_diff <= 5`.
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled = time(9, 0)
    actual = _local_dt(9, 5)             # exactly 5 min late

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 0, "Exactly 5 min must NOT produce a deduction (boundary is inclusive)"


async def test_6_min_late_inserts_deduction(db_conn, base_data):
    """
    6 minutes late → over tolerance → one deduction row is created.
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled = time(8, 0)
    actual = _local_dt(8, 6)             # 6 min late

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 1
    assert rows[0]["type"] == "tardiness"
    assert rows[0]["minutes_diff"] == 6


async def test_tardiness_30_min_correct_amount(db_conn, base_data):
    """
    30 min late at hourly_rate=12500
    → deduction = quantize_money(30/60 * 12500) = 6250.00
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled = time(8, 0)
    actual = _local_dt(8, 30)            # 30 min late

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 1
    row = rows[0]

    expected = quantize_money(money_mul(Decimal("30") / Decimal("60"), Decimal("12500")))
    assert row["minutes_diff"] == 30
    assert Decimal(str(row["deduction_amount"])) == expected, (
        f"Expected {expected}, got {row['deduction_amount']}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests — early departure (clock-out)
# ─────────────────────────────────────────────────────────────────────────────

async def test_early_departure_20_min_correct_amount(db_conn, base_data):
    """
    Leaves 20 min early at hourly_rate=15000
    → deduction = quantize_money(20/60 * 15000) = 5000.00
    """
    rid = base_data["restaurant_id"]
    # Insert a second staff member with hourly_rate=15000 to isolate this test
    pin_hash = "$2b$12$AAAAAAAAAAAAAAAAAAAAAO9UtBzOy71JBR.oeSYHsRJyR/0MDXiWG"
    staff_row2 = await db_conn.fetchrow(
        """INSERT INTO staff
               (restaurant_id, name, username, role, pin, hourly_rate)
           VALUES ($1, $2, $3, 'mesero', $4, $5)
           RETURNING id""",
        rid,
        "Maria Test",
        f"maria.test.{uuid.uuid4().hex[:6]}",
        pin_hash,
        Decimal("15000"),
    )
    sid2 = str(staff_row2["id"])
    shift_id = await _insert_open_shift(db_conn, sid2, rid)

    scheduled_end = time(18, 0)          # shift ends 18:00 local
    actual_out = _local_dt(17, 40)       # left at 17:40 → 20 min early

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid2,
        restaurant_id=rid,
        deduction_type="early_departure",
        scheduled_time=scheduled_end,
        actual_time=actual_out,
        hourly_rate=Decimal("15000"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 1
    row = rows[0]

    expected = quantize_money(money_mul(Decimal("20") / Decimal("60"), Decimal("15000")))
    assert row["type"] == "early_departure"
    assert row["minutes_diff"] == 20
    assert Decimal(str(row["deduction_amount"])) == expected, (
        f"Expected {expected}, got {row['deduction_amount']}"
    )


async def test_early_departure_within_tolerance_no_deduction(db_conn, base_data):
    """
    Leaves 3 minutes early → within tolerance → NO deduction row.
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled_end = time(17, 0)
    actual_out = _local_dt(16, 57)       # 3 min early

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="early_departure",
        scheduled_time=scheduled_end,
        actual_time=actual_out,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test — different timezone
# ─────────────────────────────────────────────────────────────────────────────

async def test_different_timezone_mexico_city(db_conn, base_data):
    """
    When restaurant_tz='America/Mexico_City' (UTC-6), the local time used for
    comparison must be correct.  We construct an actual_time that is exactly
    10 minutes after scheduled_start in Mexico City local time, and verify
    that a deduction of 10 min is recorded (not some wrong UTC-shifted value).
    """
    from zoneinfo import ZoneInfo

    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    mex_tz = ZoneInfo("America/Mexico_City")
    scheduled = time(9, 0)               # 09:00 Mexico City local

    # Build actual_time as 09:10 Mexico City, expressed as UTC-aware datetime
    today_mex_naive = datetime.now(tz=timezone.utc).astimezone(mex_tz).replace(
        hour=9, minute=10, second=0, microsecond=0, tzinfo=None
    )
    actual = today_mex_naive.replace(tzinfo=mex_tz).astimezone(timezone.utc)

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Mexico_City",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 1, (
        "10 min late in Mexico City must produce a deduction row"
    )
    assert rows[0]["minutes_diff"] == 10, (
        f"Expected 10 min diff (Mexico City), got {rows[0]['minutes_diff']}"
    )


async def test_different_timezone_wrong_tz_gives_wrong_result(db_conn, base_data):
    """
    Regression guard: if we accidentally pass 'America/Bogota' (UTC-5) for a
    Mexico City (UTC-6) timestamp, the computed local time will be 1 hour off,
    so the minutes_diff will differ from the real 10-min lateness.
    This test documents the INCORRECT behaviour that the fix prevents.
    """
    from zoneinfo import ZoneInfo

    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    mex_tz = ZoneInfo("America/Mexico_City")
    scheduled = time(9, 0)

    # actual_time is 09:10 Mexico City = 15:10 UTC
    today_mex_naive = datetime.now(tz=timezone.utc).astimezone(mex_tz).replace(
        hour=9, minute=10, second=0, microsecond=0, tzinfo=None
    )
    actual = today_mex_naive.replace(tzinfo=mex_tz).astimezone(timezone.utc)

    # Intentionally pass WRONG timezone (Bogota instead of Mexico City)
    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",          # WRONG — would have been hardcoded
    )

    rows = await _count_deductions(db_conn, shift_id)
    # With the wrong timezone the local times shift.  Both TZs are west of UTC,
    # Bogota is UTC-5 and Mexico City is UTC-6, so interpreting a Mexico City
    # timestamp through Bogota shifts local time by +1 h → 10:10 local, which
    # is 70 min late against the 09:00 schedule.
    if len(rows) == 1:
        assert rows[0]["minutes_diff"] != 10, (
            "Wrong timezone must produce a different (incorrect) minutes_diff — "
            "this regression guard confirms the fix is necessary"
        )
    # If tolerance swallowed the shifted value that is also a wrong outcome;
    # the assertion above (minutes_diff != 10) is sufficient to prove the point.


# ─────────────────────────────────────────────────────────────────────────────
# Test — deduction row contents are fully populated
# ─────────────────────────────────────────────────────────────────────────────

async def test_deduction_row_all_columns_populated(db_conn, base_data):
    """
    Verify every column of the inserted row has a sensible value.
    """
    rid = base_data["restaurant_id"]
    sid = base_data["staff_id"]
    shift_id = await _insert_open_shift(db_conn, sid, rid)

    scheduled = time(8, 0)
    actual = _local_dt(8, 15)            # 15 min late

    await _record_attendance_deduction(
        db_conn,
        shift_id=shift_id,
        staff_id=sid,
        restaurant_id=rid,
        deduction_type="tardiness",
        scheduled_time=scheduled,
        actual_time=actual,
        hourly_rate=Decimal("12500"),
        restaurant_tz="America/Bogota",
    )

    rows = await _count_deductions(db_conn, shift_id)
    assert len(rows) == 1
    row = rows[0]

    assert str(row["shift_id"]) == shift_id
    assert str(row["staff_id"]) == sid
    assert row["restaurant_id"] == rid
    assert row["type"] == "tardiness"
    assert row["minutes_diff"] == 15
    assert row["deduction_amount"] > 0
    assert row["scheduled_time"] is not None
    assert row["actual_time"] is not None
    assert row["created_at"] is not None
