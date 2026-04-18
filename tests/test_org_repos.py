"""
tests/test_org_repos.py

Unit tests for the new Org/Location repository functions added in Bloque S3.

All tests are unit tests — they mock asyncpg at the pool boundary.
No live database required.

Pattern: same as test_loyalty_repo_tenant.py — patch
  ``app.services.database.get_pool`` and supply a minimal conn mock.

The bypass_tenant_scope context manager is used internally by every new
function, so tests verify it does NOT raise TenantContextConflict even when
called from outside any tenant scope (that's by design — these are GLOBAL/
bypass-only functions).
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_context import bypass_tenant_scope, tenant_scope


# ── Mock helpers ─────────────────────────────────────────────────────────────


def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_org_row(org_id: int = 1, name: str = "Test Org") -> dict:
    return {
        "id": org_id,
        "name": name,
        "slug": "test-org",
        "whatsapp_number": "573001234567",
        "wa_phone_id": "phone_id_1",
        "wa_access_token": "token_1",
        "menu": json.dumps({"Principales": [{"name": "Bandeja", "price": 28000}]}),
        "features": json.dumps({"locale": "es-CO", "currency": "COP"}),
        "subscription_plan": "pro",
        "subscription_status": "active",
        "created_at": None,
        "updated_at": None,
    }


def _make_location_row(
    loc_id: int = 10,
    org_id: int = 1,
    name: str = "Sede Principal",
    is_primary: bool = True,
    whatsapp_number: str | None = None,
    lat: float = 4.7110,
    lon: float = -74.0721,
) -> dict:
    return {
        "id": loc_id,
        "org_id": org_id,
        "name": name,
        "code": "principal" if is_primary else "norte",
        "address": "Calle 1 #2-3",
        "latitude": lat,
        "longitude": lon,
        "whatsapp_number": whatsapp_number,
        "wa_phone_id": None,
        "wa_access_token": None,
        "active": True,
        "is_primary": is_primary,
        "timezone": "America/Bogota",
        "opening_hours": json.dumps({}),
        "created_at": None,
        "updated_at": None,
        "legacy_restaurant_id": None,
    }


# ── asyncpg Record mock ───────────────────────────────────────────────────────


class _FakeRecord(dict):
    """Minimal asyncpg Record substitute — just a dict with key access."""
    pass


# ── Test: db_get_org_by_phone matches organizations.whatsapp_number ──────────


async def test_db_get_org_by_phone_matches_organization_number():
    """When phone matches organizations.whatsapp_number, returns Org dict with
    matched_location_id=None."""
    conn = _make_conn()
    # Location query returns nothing (no override match)
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,  # locations JOIN query
            _FakeRecord(_make_org_row(org_id=5, name="Org A")),  # organizations query
        ]
    )
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_get_org_by_phone

        result = await db_get_org_by_phone("+57 300 123 4567")

    assert result is not None
    assert result["id"] == 5
    assert result["name"] == "Org A"
    assert result["matched_location_id"] is None


# ── Test: db_get_org_by_phone matches locations.whatsapp_number ──────────────


async def test_db_get_org_by_phone_matches_location_override_number():
    """When phone matches a Location's whatsapp_number, returns Org dict with
    matched_location_id set to that location's id."""
    conn = _make_conn()
    # Location query returns a row with location_id = 11 and org data
    loc_match = _FakeRecord({
        "location_id": 11,
        "id": 3,
        "name": "Cadena Norte",
        "slug": "cadena-norte",
        "whatsapp_number": "573009999888",
        "wa_phone_id": None,
        "wa_access_token": None,
        "menu": json.dumps([]),
        "features": json.dumps({"locale": "es-CO", "currency": "COP"}),
        "subscription_plan": "pro",
        "subscription_status": "active",
        "created_at": None,
        "updated_at": None,
    })
    conn.fetchrow = AsyncMock(return_value=loc_match)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_get_org_by_phone

        result = await db_get_org_by_phone("573009999888")

    assert result is not None
    assert result["id"] == 3
    assert result["matched_location_id"] == 11
    # Org's whatsapp_number in result is from organizations row, not the location
    assert result["name"] == "Cadena Norte"


# ── Test: db_get_org_by_phone normalizes phone format ────────────────────────


async def test_db_get_org_by_phone_normalizes_phone_format():
    """Ensure + and spaces are stripped before querying — the SQL also strips,
    but we test that the normalization path reaches the query with expected form."""
    conn = _make_conn()

    # Capture the phone argument passed to the first fetchrow call
    captured_args: list = []

    async def _capturing_fetchrow(sql: str, *args):
        captured_args.extend(args)
        return None  # no match

    conn.fetchrow = AsyncMock(side_effect=_capturing_fetchrow)
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_get_org_by_phone

        await db_get_org_by_phone("+57 300 123 4567")

    # First fetchrow call should receive the normalized phone (no + or spaces)
    assert captured_args, "No arguments captured — fetchrow was not called"
    assert "+" not in captured_args[0], f"Phone not normalized: {captured_args[0]!r}"
    assert " " not in captured_args[0], f"Phone has spaces: {captured_args[0]!r}"
    assert captured_args[0] == "573001234567"


# ── Test: db_get_org_locations returns primary first ─────────────────────────


async def test_db_get_org_locations_returns_primary_first():
    """db_get_org_locations returns rows ordered is_primary DESC, name ASC.
    Test that primary Location appears first in the returned list."""
    primary = _FakeRecord(_make_location_row(loc_id=10, is_primary=True, name="Principal"))
    secondary = _FakeRecord(_make_location_row(loc_id=11, is_primary=False, name="Norte"))

    conn = _make_conn()
    # SQL orders is_primary DESC, so primary comes first from DB; we replicate that
    conn.fetch = AsyncMock(return_value=[primary, secondary])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_get_org_locations

        locs = await db_get_org_locations(1)

    assert len(locs) == 2
    assert locs[0]["id"] == 10
    assert locs[0]["is_primary"] is True
    assert locs[1]["id"] == 11
    assert locs[1]["is_primary"] is False


# ── Test: db_get_primary_location returns the primary ────────────────────────


async def test_db_get_primary_location_invariant():
    """db_get_primary_location returns the single is_primary=True Location
    for an Org; returns None when no primary exists."""
    primary_row = _FakeRecord(_make_location_row(loc_id=10, is_primary=True))

    conn_with = _make_conn()
    conn_with.fetchrow = AsyncMock(return_value=primary_row)
    pool_with = _make_pool(conn_with)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool_with)):
        from app.repositories.restaurant_repo import db_get_primary_location

        result = await db_get_primary_location(1)

    assert result is not None
    assert result["id"] == 10
    assert result["is_primary"] is True

    # Now test None case
    conn_none = _make_conn()
    conn_none.fetchrow = AsyncMock(return_value=None)
    pool_none = _make_pool(conn_none)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool_none)):
        result_none = await db_get_primary_location(99)

    assert result_none is None


# ── Test: db_resolve_location_by_gps within radius ───────────────────────────


async def test_db_resolve_location_by_gps_within_radius():
    """When there is an active Location within radius_km, it is returned
    with a distance_km field."""
    # Bogotá centro ~4.6097, -74.0817
    loc_row = _FakeRecord(
        _make_location_row(loc_id=10, lat=4.6097, lon=-74.0817, is_primary=True)
    )
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[loc_row])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_resolve_location_by_gps

        # Query point ~0.5 km from Bogotá centro
        result = await db_resolve_location_by_gps(
            org_id=1, lat=4.6140, lon=-74.0810, radius_km=5.0
        )

    assert result is not None
    assert result["id"] == 10
    assert "distance_km" in result
    assert result["distance_km"] < 5.0


# ── Test: db_resolve_location_by_gps returns None when outside radius ────────


async def test_db_resolve_location_by_gps_returns_none_when_outside_radius():
    """When the nearest Location is beyond radius_km, returns None."""
    # Medellín ~6.2442, -75.5812 — far from Bogotá
    loc_row = _FakeRecord(
        _make_location_row(loc_id=10, lat=6.2442, lon=-75.5812, is_primary=True)
    )
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=[loc_row])
    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        from app.repositories.restaurant_repo import db_resolve_location_by_gps

        # Query from Bogotá centro — distance to Medellín is ~235 km
        result = await db_resolve_location_by_gps(
            org_id=1, lat=4.7110, lon=-74.0721, radius_km=5.0
        )

    assert result is None
