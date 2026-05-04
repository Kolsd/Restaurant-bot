"""
tests/test_tip_preview.py
=========================
Tests for `db_preview_tip_distribution` repo function and the
GET /api/staff/tips/preview endpoint.

Repo tests use the real DB (TEST_DATABASE_URL) — same fixture pattern as
test_tips.py / test_loyalty_aggregates.py. Endpoint tests mock the repo
layer so they run without a DB.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── _ConnProxy (works around asyncpg >=0.30 __slots__) ────────────────────────


class _ConnProxy:
    def __init__(self, conn: asyncpg.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def _make_pool_for_conn(conn):
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = AsyncMock()
    pool.acquire = _acquire
    return pool


def _uid() -> str:
    return str(uuid.uuid4())


# ── Seed helpers (mirror test_tips.py minimal seed) ──────────────────────────


async def _insert_org_and_location(conn, *, tip_distribution: dict) -> int:
    """Insert org + location with id == org_id (so location_id == org_id).

    Returns: org_id (== location_id).
    """
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number, features)
           VALUES ($1, $2, $3::jsonb)
           RETURNING id""",
        "Tip Preview Test Org",
        f"+57{uuid.uuid4().int % 10_000_000_000:010d}",
        json.dumps({"tip_distribution": tip_distribution}),
    )
    await conn.execute(
        """INSERT INTO locations (id, org_id, name, code, address, active, timezone)
           VALUES ($1, $1, 'Test Location', 'main', NULL, true, 'America/Bogota')""",
        org_id,
    )
    await conn.execute(
        "SELECT setval('locations_id_seq', GREATEST(nextval('locations_id_seq'), $1))",
        org_id,
    )
    return org_id


async def _insert_staff(conn, *, restaurant_id: int, name: str, role: str) -> str:
    sid = uuid.uuid4()
    unique_username = f"pv_{uuid.uuid4().hex[:10]}"
    await conn.execute(
        """INSERT INTO staff (id, org_id, name, role, pin, username)
           VALUES ($1, $2, $3, $4, '', $5)""",
        sid, restaurant_id, name, role, unique_username,
    )
    return str(sid)


async def _insert_open_shift(
    conn, *, staff_id: str, restaurant_id: int, started_iso: str
) -> None:
    await conn.execute(
        """INSERT INTO staff_shifts (id, staff_id, org_id, clock_in, clock_out)
           VALUES ($1, $2::uuid, $3, $4, NULL)""",
        uuid.uuid4(), staff_id, restaurant_id,
        datetime.fromisoformat(started_iso),
    )


# ── Test 1: 1 mesero on shift gets 100% of tip when only mesero in config ────


@_needs_db
@pytest.mark.asyncio
async def test_preview_with_staff_on_shift_returns_splits(db_conn):
    org_id = await _insert_org_and_location(
        db_conn, tip_distribution={"mesero": 100}
    )
    juan = await _insert_staff(db_conn, restaurant_id=org_id, name="Juan", role="mesero")
    # Open shift starting 2h ago
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_open_shift(
        db_conn, staff_id=juan, restaurant_id=org_id, started_iso=started
    )

    pool = _make_pool_for_conn(_ConnProxy(db_conn))
    with patch("app.services.database.get_pool", return_value=pool):
        with tenant_scope(org_id):
            from app.repositories.staff_repo import db_preview_tip_distribution
            result = await db_preview_tip_distribution(
                restaurant_id=org_id,
                tip_amount=Decimal("10000"),
            )

    assert result["tip_amount"] == 10000.0
    assert len(result["splits"]) == 1, f"expected 1 split, got {result['splits']!r}"
    split = result["splits"][0]
    assert split["staff_id"] == juan
    assert split["name"] == "Juan"
    assert split["role"] == "mesero"
    assert split["amount"] == 10000.0
    assert result["unallocated"] == 0.0
    assert result["config"] == {"mesero": 100}


# ── Test 2: 0 staff on shift → entire tip is unallocated ─────────────────────


@_needs_db
@pytest.mark.asyncio
async def test_preview_with_no_staff_returns_unallocated(db_conn):
    org_id = await _insert_org_and_location(
        db_conn, tip_distribution={"mesero": 50, "cocina": 50}
    )
    # No staff inserted, no shifts — empty restaurant.

    pool = _make_pool_for_conn(_ConnProxy(db_conn))
    with patch("app.services.database.get_pool", return_value=pool):
        with tenant_scope(org_id):
            from app.repositories.staff_repo import db_preview_tip_distribution
            result = await db_preview_tip_distribution(
                restaurant_id=org_id,
                tip_amount=Decimal("8000"),
            )

    assert result["tip_amount"] == 8000.0
    assert result["splits"] == []
    assert result["unallocated"] == 8000.0
    assert result["config"] == {"mesero": 50, "cocina": 50}


# ── Test 3: Redistribution when only some configured roles have staff ────────


@_needs_db
@pytest.mark.asyncio
async def test_preview_redistributes_when_role_missing(db_conn):
    """Config: mesero=50, cocina=50.  Only mesero on shift.
    Mesero should get 100% (50/50 = full pot)."""
    org_id = await _insert_org_and_location(
        db_conn, tip_distribution={"mesero": 50, "cocina": 50}
    )
    pedro = await _insert_staff(db_conn, restaurant_id=org_id, name="Pedro", role="mesero")
    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _insert_open_shift(
        db_conn, staff_id=pedro, restaurant_id=org_id, started_iso=started
    )

    pool = _make_pool_for_conn(_ConnProxy(db_conn))
    with patch("app.services.database.get_pool", return_value=pool):
        with tenant_scope(org_id):
            from app.repositories.staff_repo import db_preview_tip_distribution
            result = await db_preview_tip_distribution(
                restaurant_id=org_id,
                tip_amount=Decimal("12000"),
            )

    assert len(result["splits"]) == 1
    assert result["splits"][0]["staff_id"] == pedro
    # Mesero gets full pot since cocina has no one
    assert result["splits"][0]["amount"] == 12000.0
    assert result["unallocated"] == 0.0


# ── Test 4: Endpoint validates amount range (no DB needed — pure logic) ──────


@pytest.mark.asyncio
async def test_endpoint_validates_amount_zero():
    """amount=0 → 400."""
    from app.routes.staff import preview_tip_distribution
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await preview_tip_distribution(
            amount=Decimal("0"),
            when=None,
            restaurant={"id": 1},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_validates_amount_negative():
    from app.routes.staff import preview_tip_distribution
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await preview_tip_distribution(
            amount=Decimal("-5"),
            when=None,
            restaurant={"id": 1},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_endpoint_validates_amount_too_large():
    from app.routes.staff import preview_tip_distribution
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await preview_tip_distribution(
            amount=Decimal("10000001"),
            when=None,
            restaurant={"id": 1},
        )
    assert exc.value.status_code == 400


# ── Test 5: Decimal precision — splits sum exactly to input ──────────────────


@_needs_db
@pytest.mark.asyncio
async def test_endpoint_decimal_precision(db_conn):
    """3 staff (different roles), config evenly split, amount with decimals.
    Splits + unallocated must sum exactly to the input amount.
    """
    org_id = await _insert_org_and_location(
        db_conn, tip_distribution={"mesero": 40, "cocina": 35, "bar": 25}
    )
    juan = await _insert_staff(db_conn, restaurant_id=org_id, name="Juan", role="mesero")
    maria = await _insert_staff(db_conn, restaurant_id=org_id, name="Maria", role="cocina")
    luis = await _insert_staff(db_conn, restaurant_id=org_id, name="Luis", role="bar")

    started = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for sid in (juan, maria, luis):
        await _insert_open_shift(
            db_conn, staff_id=sid, restaurant_id=org_id, started_iso=started
        )

    pool = _make_pool_for_conn(_ConnProxy(db_conn))
    with patch("app.services.database.get_pool", return_value=pool):
        with tenant_scope(org_id):
            from app.repositories.staff_repo import db_preview_tip_distribution
            # Use a value that doesn't divide cleanly by 100 (40/35/25)
            tip_input = Decimal("9999")
            result = await db_preview_tip_distribution(
                restaurant_id=org_id,
                tip_amount=tip_input,
            )

    # All 3 staff should appear
    assert len(result["splits"]) == 3
    # Sum of splits + unallocated must equal input (within quantize_money rounding)
    total_split = sum(s["amount"] for s in result["splits"])
    total = total_split + result["unallocated"]
    assert abs(total - 9999.0) < 0.01, (
        f"splits ({total_split}) + unallocated ({result['unallocated']}) = {total}, "
        f"expected 9999.0"
    )
    # No staff_id should appear twice
    seen = set()
    for s in result["splits"]:
        assert s["staff_id"] not in seen
        seen.add(s["staff_id"])
