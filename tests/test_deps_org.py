"""
tests/test_deps_org.py

Verify that the new Org/Location FastAPI dependencies (Bloque S3) work
correctly and that existing legacy deps are unchanged.

All tests are unit tests — asyncpg pool and downstream repos are mocked.
No live database required.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException


# ── Fake request helper ───────────────────────────────────────────────────────


def _make_request(headers: dict | None = None, state_attrs: dict | None = None):
    """Build a minimal Request-like object for dep testing."""
    req = MagicMock()
    req.headers = {**(headers or {})}
    state = MagicMock()
    state_attrs = state_attrs or {}
    # getattr(req.state, "mesio_org", None) should return None unless preset
    state.mesio_org = state_attrs.get("mesio_org", None)
    req.state = state
    return req


# ── Mock helpers ──────────────────────────────────────────────────────────────


def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
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


_ORG = {
    "id": 1,
    "name": "Test Org",
    "whatsapp_number": "573001234567",
    "features": {"locale": "es-CO", "currency": "COP"},
    "subscription_plan": "pro",
    "subscription_status": "active",
}

_LOC_PRIMARY = {
    "id": 10,
    "org_id": 1,
    "name": "Principal",
    "is_primary": True,
    "whatsapp_number": None,
    "active": True,
}

_LOC_OTHER_ORG = {
    "id": 99,
    "org_id": 999,  # different org!
    "name": "Otro Restaurante",
    "is_primary": True,
    "whatsapp_number": None,
    "active": True,
}

_USER_ADMIN = {
    "username": "owner@test.com",
    "restaurant_id": 1,
    "branch_id": 1,
    "role": "owner",
}

_USER_STAFF_BRANCH = {
    "username": "staff:uuid-abc",
    "restaurant_id": 20,
    "branch_id": 20,
    "role": "mesero",
}


# ── test_get_current_org_resolves_org_for_matriz_user ────────────────────────


async def test_get_current_org_resolves_org_for_matriz_user():
    """For a Matriz user (restaurant_id == org_id), get_current_org returns Org dict."""
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=_FakeRecord({"org_id": 1}))
    pool = _make_pool(conn)

    request = _make_request()

    with (
        patch("app.routes.deps.db.get_pool", AsyncMock(return_value=pool)),
        patch(
            "app.routes.deps.get_current_user",
            AsyncMock(return_value=_USER_ADMIN),
        ),
        patch(
            "app.routes.deps.db.db_get_org_by_id",
            AsyncMock(return_value=_ORG),
        ),
    ):
        from app.routes.deps import get_current_org

        org = await get_current_org(request)

    assert org["id"] == 1
    assert org["name"] == "Test Org"
    # Result should be cached on request.state
    assert request.state.mesio_org is not None


# ── test_get_current_org_resolves_org_for_sucursal_user ──────────────────────


async def test_get_current_org_resolves_org_for_sucursal_user():
    """For a Sucursal staff user (restaurant_id = 20, mapped to org_id = 1),
    get_current_org correctly returns the parent Org."""
    conn = _make_conn()
    # Mapping lookup returns org_id=1 for restaurant_id=20
    conn.fetchrow = AsyncMock(return_value=_FakeRecord({"org_id": 1}))
    pool = _make_pool(conn)

    request = _make_request()

    with (
        patch("app.routes.deps.db.get_pool", AsyncMock(return_value=pool)),
        patch(
            "app.routes.deps.get_current_user",
            AsyncMock(return_value=_USER_STAFF_BRANCH),
        ),
        patch(
            "app.routes.deps.db.db_get_org_by_id",
            AsyncMock(return_value=_ORG),
        ),
    ):
        from app.routes.deps import get_current_org

        org = await get_current_org(request)

    assert org["id"] == 1
    assert org["name"] == "Test Org"


# ── test_get_current_location_validates_ownership ────────────────────────────


async def test_get_current_location_validates_ownership():
    """get_current_location returns the Location when X-Location-ID belongs to the Org."""
    request = _make_request(headers={"X-Location-ID": "10"})

    with patch(
        "app.routes.deps.db.db_get_location_by_id",
        AsyncMock(return_value=_LOC_PRIMARY),
    ):
        from app.routes.deps import get_current_location

        loc = await get_current_location(request, org=_ORG)

    assert loc is not None
    assert loc["id"] == 10
    assert loc["org_id"] == 1


# ── test_get_current_location_rejects_cross_org ──────────────────────────────


async def test_get_current_location_rejects_cross_org():
    """get_current_location raises 403 when X-Location-ID belongs to a different Org."""
    request = _make_request(headers={"X-Location-ID": "99"})

    with patch(
        "app.routes.deps.db.db_get_location_by_id",
        AsyncMock(return_value=_LOC_OTHER_ORG),
    ):
        from app.routes.deps import get_current_location

        with pytest.raises(HTTPException) as exc_info:
            await get_current_location(request, org=_ORG)

    assert exc_info.value.status_code == 403


# ── test_get_current_location_returns_none_for_all ───────────────────────────


async def test_get_current_location_returns_none_for_missing_header():
    """get_current_location returns None when header is absent (queries all locations)."""
    request = _make_request(headers={})  # no X-Location-ID

    from app.routes.deps import get_current_location

    loc = await get_current_location(request, org=_ORG)

    assert loc is None


async def test_get_current_location_returns_none_for_all():
    """get_current_location returns None when X-Location-ID is 'all'."""
    request = _make_request(headers={"X-Location-ID": "all"})

    from app.routes.deps import get_current_location

    loc = await get_current_location(request, org=_ORG)

    assert loc is None


# ── test_get_current_restaurant_legacy_still_returns_data ────────────────────


async def test_get_current_restaurant_legacy_still_returns_data():
    """Existing get_current_restaurant dep still works after Bloque S3 changes.
    Routes that use this dep should not be affected."""
    _restaurant = {
        "id": 1,
        "name": "Test Restaurant",
        "whatsapp_number": "573001234567",
        "features": {"locale": "es-CO", "currency": "COP"},
    }

    request = _make_request(headers={})

    with (
        patch(
            "app.routes.deps.get_current_user",
            AsyncMock(
                return_value={
                    "username": "owner@test.com",
                    "branch_id": 1,
                    "restaurant_id": 1,
                    "role": "owner",
                }
            ),
        ),
        patch(
            "app.routes.deps.db.db_get_restaurant_by_id",
            AsyncMock(return_value=_restaurant),
        ),
    ):
        from app.routes.deps import get_current_restaurant

        restaurant = await get_current_restaurant(request)

    assert restaurant is not None
    assert restaurant["id"] == 1
    assert restaurant["name"] == "Test Restaurant"
    assert "whatsapp_number" in restaurant


# ── test_require_location_raises_when_none ───────────────────────────────────


async def test_require_location_raises_when_none():
    """require_location raises 400 when no X-Location-ID header is provided."""
    from app.routes.deps import require_location

    with pytest.raises(HTTPException) as exc_info:
        await require_location(request=MagicMock(), location=None)

    assert exc_info.value.status_code == 400
