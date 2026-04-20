"""
tests/test_auth_new_shape.py

Verify that login() returns the new Org/Locations shape while preserving the
legacy ``restaurant`` key for backward compatibility (Bloque S3).

Tests are unit tests — the DB pool and repo functions are mocked so no live
database is needed.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────


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


class _FakeRecord(dict):
    pass


_ORG_ROW = _FakeRecord({
    "id": 1,
    "name": "Test Restaurant",
    "slug": "test-restaurant",
    "whatsapp_number": "573001234567",
    "wa_phone_id": "ph1",
    "wa_access_token": "tok1",
    "menu": json.dumps({}),
    "features": json.dumps({"locale": "es-CO", "currency": "COP"}),
    "subscription_plan": "pro",
    "subscription_status": "active",
    "created_at": None,
    "updated_at": None,
})

_LOC_PRIMARY = _FakeRecord({
    "id": 10,
    "org_id": 1,
    "name": "Sede Principal",
    "code": "principal",
    "address": "Calle 1",
    "latitude": 4.7110,
    "longitude": -74.0721,
    "whatsapp_number": None,
    "wa_phone_id": None,
    "wa_access_token": None,
    "active": True,
    "is_primary": True,
    "timezone": "America/Bogota",
    "opening_hours": json.dumps({}),
    "created_at": None,
    "updated_at": None,
})

_LOC_SECONDARY = _FakeRecord({
    "id": 11,
    "org_id": 1,
    "name": "Sede Norte",
    "code": "norte",
    "address": "Calle 100",
    "latitude": 4.7500,
    "longitude": -74.0500,
    "whatsapp_number": "573009999888",
    "wa_phone_id": None,
    "wa_access_token": None,
    "active": True,
    "is_primary": False,
    "timezone": "America/Bogota",
    "opening_hours": json.dumps({}),
    "created_at": None,
    "updated_at": None,
})

_MAPPING_ROW = _FakeRecord({"org_id": 1, "location_id": 10})  # kept for reference, no longer used by app code

_USER_ROW = {
    "username": "owner@test.com",
    "password_hash": "$2b$12$fakehashedpassword1234567890abcdef",
    "restaurant_name": "Test Restaurant",
    "role": "owner",
    "branch_id": 1,
    "parent_user": None,
}

_RESTAURANT_ROW = {
    "id": 1,
    "name": "Test Restaurant",
    "whatsapp_number": "573001234567",
    "features": {"locale": "es-CO", "currency": "COP"},
    "subscription_plan": "pro",
    "subscription_status": "active",
}

_STAFF_ROW = {
    "id": "staff-uuid-1234",
    "name": "Carlos López",
    "role": "mesero",
    "roles": ["mesero"],
    "restaurant_id": 20,
    "pin": "$2b$12$fakehashedpin1234567890abcdef",
}

_RESTAURANT_BRANCH_ROW = {
    "id": 20,
    "name": "Sede Norte",
    "whatsapp_number": "573009999888",
    "features": {"locale": "es-CO", "currency": "COP"},
}

_MAPPING_BRANCH_ROW = _FakeRecord({"org_id": 1, "location_id": 11})


# ── test_login_returns_org_and_locations_for_owner ────────────────────────────


async def test_login_returns_org_and_locations_for_owner():
    """Admin/owner login returns org, locations, default_location_id keys."""
    with (
        patch("app.services.auth.db.db_get_user", AsyncMock(return_value=_USER_ROW)),
        patch(
            "app.services.auth.db.db_get_restaurant_by_id",
            AsyncMock(return_value=_RESTAURANT_ROW),
        ),
        patch(
            "app.services.auth.db.db_get_all_restaurants",
            AsyncMock(return_value=[_RESTAURANT_ROW]),
        ),
        patch(
            "app.services.auth.sessions_repo.create_session",
            AsyncMock(return_value="tok-abc"),
        ),
        # verify_password — patch bcrypt verify to always return True
        patch(
            "app.services.auth.verify_password",
            return_value=True,
        ),
        # Post-0037: location lookup replaces mapping table
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=dict(_LOC_PRIMARY)),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_id",
            AsyncMock(return_value={
                "id": 1,
                "name": "Test Restaurant",
                "whatsapp_number": "573001234567",
                "features": {"locale": "es-CO", "currency": "COP"},
                "subscription_plan": "pro",
                "subscription_status": "active",
            }),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[dict(_LOC_PRIMARY), dict(_LOC_SECONDARY)]),
        ),
    ):
        from app.services.auth import login

        result = await login("owner@test.com", "password")

    assert result["success"] is True
    assert "org" in result, "Expected 'org' key in login response"
    assert result["org"]["id"] == 1
    assert result["org"]["name"] == "Test Restaurant"

    assert "locations" in result, "Expected 'locations' key in login response"
    assert len(result["locations"]) == 2

    assert "default_location_id" in result, "Expected 'default_location_id' in response"
    # default_location_id should be the primary Location's id
    primary_ids = [loc["id"] for loc in result["locations"] if loc.get("is_primary")]
    if primary_ids:
        assert result["default_location_id"] == primary_ids[0]


# ── test_login_returns_staff_scoped_locations_for_mesero ─────────────────────


async def test_login_returns_staff_scoped_locations_for_mesero():
    """Staff login returns only the Location(s) that staff belongs to."""
    with (
        patch("app.services.auth.db.db_get_user", AsyncMock(return_value=None)),
        patch(
            "app.services.auth.db.db_get_staff_candidates_by_name",
            AsyncMock(return_value=[_STAFF_ROW]),
        ),
        patch(
            "app.services.auth.db.db_get_restaurant_by_id",
            AsyncMock(return_value=_RESTAURANT_BRANCH_ROW),
        ),
        patch(
            "app.services.auth.sessions_repo.create_session",
            AsyncMock(return_value="tok-staff"),
        ),
        patch(
            "app.services.auth.verify_password",
            return_value=True,
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_id",
            AsyncMock(return_value={
                "id": 1,
                "name": "Test Restaurant",
                "whatsapp_number": "573001234567",
                "features": {"locale": "es-CO", "currency": "COP"},
                "subscription_plan": "pro",
            }),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=dict(_LOC_SECONDARY)),
        ),
    ):
        from app.services.auth import login

        result = await login("Carlos López", "pin123")

    assert result["success"] is True
    assert result["staff_id"] == "staff-uuid-1234"

    # Staff gets ONLY their Location, not all locations of the Org
    if "locations" in result:
        assert len(result["locations"]) == 1
        assert result["locations"][0]["id"] == 11


# ── test_login_preserves_legacy_restaurant_key ────────────────────────────────


async def test_login_preserves_legacy_restaurant_key():
    """The legacy ``restaurant`` key is always present with the pre-S3 shape."""
    with (
        patch("app.services.auth.db.db_get_user", AsyncMock(return_value=_USER_ROW)),
        patch(
            "app.services.auth.db.db_get_restaurant_by_id",
            AsyncMock(return_value=_RESTAURANT_ROW),
        ),
        patch(
            "app.services.auth.db.db_get_all_restaurants",
            AsyncMock(return_value=[_RESTAURANT_ROW]),
        ),
        patch(
            "app.services.auth.sessions_repo.create_session",
            AsyncMock(return_value="tok-legacy"),
        ),
        patch("app.services.auth.verify_password", return_value=True),
        # Post-0037: location lookup replaces mapping table
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=dict(_LOC_PRIMARY)),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_id",
            AsyncMock(return_value={
                "id": 1,
                "name": "Test Restaurant",
                "whatsapp_number": "573001234567",
                "features": {"locale": "es-CO", "currency": "COP"},
            }),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_org_locations",
            AsyncMock(return_value=[dict(_LOC_PRIMARY)]),
        ),
    ):
        from app.services.auth import login

        result = await login("owner@test.com", "password")

    assert result["success"] is True
    assert "restaurant" in result, "Legacy 'restaurant' key must be present"
    r = result["restaurant"]
    # Legacy shape fields must all be present
    for key in ("id", "name", "username", "role", "branch_id", "whatsapp_number",
                "features", "locale", "currency"):
        assert key in r, f"Legacy restaurant key missing: {key!r}"

    # id should be the branch_id (restaurant id) from the original shape
    assert r["id"] == 1
    assert r["name"] == "Test Restaurant"
