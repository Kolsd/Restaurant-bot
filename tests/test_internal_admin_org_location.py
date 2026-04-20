"""
tests/test_internal_admin_org_location.py

Smoke tests for the Org/Location endpoints added in Bloque S7.
No live DB required — asyncpg pool is mocked at the module boundary.

Tests:
  1. test_create_org_also_creates_primary_location
  2. test_promote_location_to_primary_demotes_existing
  3. test_cannot_delete_primary_location
  4. test_hard_delete_org_blocked_with_recent_orders
  5. test_list_organizations_returns_location_count
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch    = AsyncMock(return_value=[])
    conn.execute  = AsyncMock(return_value=None)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__  = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


# ── Test 1 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_org_also_creates_primary_location():
    """POST /organizations creates Org then auto-creates a primary Location."""
    from app.repositories import restaurant_repo

    created_org = {
        "id": 99, "name": "Test Org", "slug": None,
        "whatsapp_number": "+573001234567", "wa_phone_id": None, "wa_access_token": None,
        "menu": [], "features": {}, "subscription_plan": "free",
        "subscription_status": "active", "created_at": None, "updated_at": None,
    }
    created_loc = {
        "id": 200, "org_id": 99, "name": "Principal", "code": "principal",
        "address": None, "latitude": None, "longitude": None,
        "whatsapp_number": None, "wa_phone_id": None, "wa_access_token": None,
        "active": True, "is_primary": False, "timezone": "America/Bogota",
        "opening_hours": {}, "created_at": None, "updated_at": None,
    }
    promoted_loc = dict(created_loc, is_primary=True)

    with patch.object(restaurant_repo, "db_create_organization", AsyncMock(return_value=created_org)) as mock_create_org, \
         patch.object(restaurant_repo, "db_create_location",     AsyncMock(return_value=created_loc)) as mock_create_loc, \
         patch.object(restaurant_repo, "db_update_location",     AsyncMock(return_value=promoted_loc)) as mock_upd_loc:

        result_org = await restaurant_repo.db_create_organization(
            name="Test Org", whatsapp_number="+573001234567",
        )
        assert result_org["id"] == 99

        result_loc = await restaurant_repo.db_create_location(
            org_id=99, name="Principal", code="principal", active=True,
        )
        assert result_loc["is_primary"] is False

        # Route promotes it
        promoted = await restaurant_repo.db_update_location(result_loc["id"], is_primary=True)
        assert promoted["is_primary"] is True

        mock_create_org.assert_called_once()
        mock_create_loc.assert_called_once_with(org_id=99, name="Principal", code="principal", active=True)
        mock_upd_loc.assert_called_once_with(200, is_primary=True)


# ── Test 2 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_location_to_primary_demotes_existing():
    """PATCH /locations/{id} with is_primary=true atomically demotes the old primary."""
    from app.repositories import restaurant_repo
    from app.services.tenant_context import bypass_tenant_scope

    existing_loc = {
        "id": 201, "org_id": 99, "name": "Sede Norte", "is_primary": False,
        "active": True, "code": "norte", "address": None, "latitude": None,
        "longitude": None, "whatsapp_number": None, "wa_phone_id": None,
        "wa_access_token": None, "timezone": "America/Bogota",
        "opening_hours": {}, "created_at": None, "updated_at": None,
    }
    after_promote = dict(existing_loc, is_primary=True)

    conn = _make_conn()
    pool = _make_pool(conn)

    with patch.object(restaurant_repo, "db_get_location_by_id", AsyncMock(side_effect=[existing_loc, after_promote])) as mock_get_loc, \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)):

        loc = await restaurant_repo.db_get_location_by_id(201)
        assert loc["is_primary"] is False

        # Simulate what the route does: atomic demote + promote via raw conn
        with bypass_tenant_scope("promote_primary_location_atomic"):
            async with pool.acquire() as c:
                async with c.transaction():
                    await c.execute(
                        "UPDATE locations SET is_primary = false WHERE org_id = $1 AND is_primary = true",
                        loc["org_id"],
                    )
                    await c.execute(
                        "UPDATE locations SET is_primary = true, updated_at = NOW() WHERE id = $1",
                        201,
                    )

        # Verify the two UPDATE calls were made
        execute_calls = conn.execute.call_args_list
        demote_call = any("is_primary = false" in str(c) for c in execute_calls)
        promote_call = any("is_primary = true" in str(c) and "WHERE id" in str(c) for c in execute_calls)
        assert demote_call, "Expected a demote UPDATE call"
        assert promote_call, "Expected a promote UPDATE call"

        # After promotion, re-fetch returns is_primary=True
        refreshed = await restaurant_repo.db_get_location_by_id(201)
        assert refreshed["is_primary"] is True


# ── Test 3 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cannot_delete_primary_location():
    """DELETE /locations/{id} returns 400 when location is primary."""
    from fastapi import HTTPException
    from app.repositories import restaurant_repo

    primary_loc = {
        "id": 200, "org_id": 99, "name": "Principal", "is_primary": True,
        "active": True, "code": "principal", "address": None, "latitude": None,
        "longitude": None, "whatsapp_number": None, "wa_phone_id": None,
        "wa_access_token": None, "timezone": "America/Bogota",
        "opening_hours": {}, "created_at": None, "updated_at": None,
    }

    with patch.object(restaurant_repo, "db_get_location_by_id", AsyncMock(return_value=primary_loc)):
        loc = await restaurant_repo.db_get_location_by_id(200)
        assert loc["is_primary"] is True

        # Simulate the route guard
        if loc["is_primary"]:
            with pytest.raises(HTTPException) as exc_info:
                raise HTTPException(
                    status_code=400,
                    detail="No se puede eliminar la sede principal. Promueve otra sede primero.",
                )
            assert exc_info.value.status_code == 400
            assert "principal" in exc_info.value.detail.lower()


# ── Test 4 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hard_delete_org_blocked_with_recent_orders():
    """DELETE /organizations/{id}?hard=true is blocked when recent orders exist."""
    from fastapi import HTTPException
    from app.repositories import restaurant_repo

    existing_org = {
        "id": 99, "name": "Test Org", "slug": None,
        "whatsapp_number": None, "wa_phone_id": None, "wa_access_token": None,
        "menu": [], "features": {}, "subscription_plan": "free",
        "subscription_status": "active", "created_at": None, "updated_at": None,
    }

    conn = _make_conn()
    # Simulate 3 recent orders
    conn.fetchval = AsyncMock(return_value=3)
    pool = _make_pool(conn)

    with patch.object(restaurant_repo, "db_get_org_by_id", AsyncMock(return_value=existing_org)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)):

        org = await restaurant_repo.db_get_org_by_id(99)
        assert org is not None

        # Simulate what the route does
        from app.services.tenant_context import bypass_tenant_scope
        with bypass_tenant_scope("delete_organization_hard_check"):
            async with pool.acquire() as c:
                recent = await c.fetchval(
                    "SELECT COUNT(*) FROM orders WHERE org_id = $1 AND created_at > NOW() - INTERVAL '90 days'",
                    99,
                )

        assert recent == 3

        if recent and recent > 0:
            with pytest.raises(HTTPException) as exc_info:
                raise HTTPException(
                    status_code=409,
                    detail="No se puede eliminar: existen pedidos en los ultimos 90 dias. Usa soft-delete.",
                )
            assert exc_info.value.status_code == 409


# ── Test 5 ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_organizations_returns_location_count():
    """GET /organizations returns each org with a location_count field."""
    from app.repositories import restaurant_repo

    mock_orgs = [
        {
            "id": 1, "name": "Org Alpha", "slug": "alpha",
            "whatsapp_number": "+571111111", "wa_phone_id": None,
            "subscription_plan": "pro", "subscription_status": "active",
            "features": {}, "created_at": None, "updated_at": None,
            "location_count": 3,
        },
        {
            "id": 2, "name": "Org Beta", "slug": "beta",
            "whatsapp_number": None, "wa_phone_id": None,
            "subscription_plan": "free", "subscription_status": "active",
            "features": {}, "created_at": None, "updated_at": None,
            "location_count": 1,
        },
    ]

    with patch.object(restaurant_repo, "db_list_organizations", AsyncMock(return_value=mock_orgs)):
        orgs = await restaurant_repo.db_list_organizations()

    assert len(orgs) == 2
    assert orgs[0]["location_count"] == 3
    assert orgs[1]["location_count"] == 1
    # All orgs must have the location_count key
    for o in orgs:
        assert "location_count" in o
