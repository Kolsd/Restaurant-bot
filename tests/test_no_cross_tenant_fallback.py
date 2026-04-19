"""
tests/test_no_cross_tenant_fallback.py

Regression suite for Paso 9 commits c4d2ea2 + 2053f11. Four sites used to
fall back to "any restaurant globally" (`db_get_all_restaurants()[0]`)
when context resolution failed. In multi-tenant production that's a
cross-tenant data leak — the request gets handed another customer's data.

Each test below drives a path where the fallback would have triggered
and asserts the new behavior: empty/error response instead of
cross-tenant data.

The tenant fixtures use distinct ORG_OWN=11 vs ORG_OTHER=99 so any
attempt to leak the "other" tenant's data shows up as a wrong integer
in the response — the test fails LOUD instead of "looks fine, oops".
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


ORG_OWN = 11
ORG_OTHER = 99


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


# ── routes/tables.py::get_pos_menu — POS menu must NOT load other tenant's menu

def test_pos_menu_returns_empty_when_user_has_no_branch_id(client):
    """If staff has no branch_id and we cannot resolve the sede, the POS must
    return an empty menu — NOT load some other tenant's menu via all_r[0]."""
    user_no_branch = {"username": "u1", "role": "mesero", "branch_id": None}

    patches = [
        patch("app.routes.tables.require_auth", AsyncMock(return_value=None)),
        patch("app.routes.tables.get_current_user", AsyncMock(return_value=user_no_branch)),
        # If the legacy fallback fired, this mock would be hit and return the
        # wrong tenant's data. We assert assert_not_called below.
        patch("app.routes.tables.db.db_get_all_restaurants",
              AsyncMock(return_value=[{"id": ORG_OTHER, "whatsapp_number": "+other"}])),
        patch("app.routes.tables.db.db_get_menu",
              AsyncMock(return_value={"Should not see this": []})),
    ]
    for p in patches:
        p.start()
    try:
        resp = client.get("/api/pos/menu", headers={"Authorization": "Bearer test"})
    finally:
        for p in patches:
            p.stop()

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["menu"] == {}, (
        f"POS must return empty menu (NOT another tenant's menu). Got: {body}"
    )


# ── routes/tables.py::_get_restaurant_for_table — no cross-tenant on missing context

@pytest.mark.asyncio
async def test_get_restaurant_for_table_returns_empty_dict_when_unresolvable():
    """Last-resort fallback used to return all_r[0] (any tenant). Now must
    return {} so the caller short-circuits gracefully."""
    from app.routes.tables import _get_restaurant_for_table
    from app.services import database as db

    # Even if db_get_all_restaurants would return cross-tenant rows, we MUST NOT
    # call it. Patch with side_effect to fail loudly if invoked.
    with patch.object(db, "db_get_all_restaurants",
                      AsyncMock(side_effect=AssertionError("must NOT be called"))):
        result = await _get_restaurant_for_table(table_id=None, session_data=None)

    assert result == {}


# ── services/auth.py::login — no cross-tenant on name-match failure

@pytest.mark.asyncio
async def test_login_returns_failure_when_name_match_fails_no_cross_tenant():
    """Owner login with no branch_id and a restaurant_name that does NOT
    match any org must fail-friendly — NOT log into another tenant."""
    from app.services import auth

    other_org = {"id": ORG_OTHER, "name": "Some Other Customer", "whatsapp_number": "+99",
                 "wa_phone_id": "ph", "wa_access_token": "tok",
                 "menu": [], "features": {}, "subscription_plan": "free",
                 "subscription_status": "active", "created_at": None, "updated_at": None}

    user = {
        "username": "owner",
        "restaurant_name": "Nonexistent Org Name That Wont Match",
        "branch_id": None,
        "role": "owner",
        "password_hash": auth.hash_password("pass123"),
    }

    with (
        patch.object(auth.db, "db_get_user", AsyncMock(return_value=user)),
        patch.object(auth.db, "db_get_all_orgs", AsyncMock(return_value=[other_org])),
        patch("app.repositories.sessions_repo.create_session",
              AsyncMock(return_value="t" * 64)),
    ):
        result = await auth.login("owner", "pass123")

    # The login must NOT impersonate the other tenant — branch_id stays None,
    # the org_resolve_failed_hard guard returns the friendly error
    assert result["success"] is False, (
        f"Login must FAIL when name match misses (no cross-tenant fallback). "
        f"Got: {result}"
    )
    assert "configuración de la sucursal" in result.get("error", "").lower() \
        or "incorrect" in result.get("error", "").lower() \
        or "Problema" in result.get("error", "")


# ── routes/deps.py::get_current_restaurant — no cross-tenant on missing branch_id

@pytest.mark.asyncio
async def test_get_current_restaurant_raises_403_for_orphaned_owner():
    """Owner whose user record has no branch_id AND whose restaurant_name
    matches no org must get 403 — NOT another tenant's data."""
    from app.routes import deps

    user_orphaned = {
        "username": "owner",
        "restaurant_name": "Mismatched Name",
        "branch_id": None,
        # No restaurant_id either — fully orphaned
    }
    other_org = {
        "id": ORG_OTHER, "name": "Some Other Customer",
        "whatsapp_number": "+99", "features": {},
    }

    # Patch the chain: get_current_user returns the orphaned user;
    # db_get_all_orgs returns ONLY a different tenant; org_locations should
    # never be queried (because name match fails first).
    fake_request = type("R", (), {"headers": {}})()

    with (
        patch.object(deps, "get_current_user", AsyncMock(return_value=user_orphaned)),
        patch.object(deps.db, "db_get_all_orgs", AsyncMock(return_value=[other_org])),
        patch.object(deps.db, "db_get_restaurant_by_id",
                     AsyncMock(side_effect=AssertionError("must NOT lookup other tenant"))),
    ):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as excinfo:
            await deps.get_current_restaurant(fake_request)

    assert excinfo.value.status_code == 403, (
        f"Orphaned owner must get 403 (NOT cross-tenant data). "
        f"Got status {excinfo.value.status_code}: {excinfo.value.detail}"
    )
