"""
tests/test_parent_restaurant_id_migration.py

Regression suite for Wave-2 parent_restaurant_id → org_id cleanup:
  - WebAuthn credential delete ownership check (staff_webauthn.py)
  - team_routes delete_branch cross-tenant guard
  - team_routes list_team_users cross-tenant guard

Covers the HIGH-priority bug where cred.get("restaurant_id") was always
None (the field is actually org_id), causing 403 on every delete attempt.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


ORG_A = 11   # org that owns the credential / branch
ORG_B = 99   # foreign org — should be blocked
LOCATION_A = 22


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


# ─── WebAuthn fixtures ────────────────────────────────────────────────────────

def _webauthn_cred(org_id):
    """Build a minimal credential dict as returned by db_get_webauthn_credential."""
    return {
        "id": "cred-uuid-1",
        "staff_id": "staff-uuid-1",
        "credential_id": "base64-cred-id",
        "public_key": "pk",
        "sign_count": 0,
        "transports": [],
        "org_id": org_id,
        "staff_name": "Test Staff",
    }


def _restaurant_dict(org_id):
    return {
        "id": org_id, "org_id": org_id, "location_id": LOCATION_A,
        "name": "Test Restaurant", "features": {"module_webauthn": True},
    }


@pytest.fixture(autouse=True)
def override_webauthn_deps(monkeypatch):
    """Override auth deps for the webauthn router for all tests in this module."""
    from app.main import app
    from app.routes.deps import get_current_restaurant

    restaurant_a = _restaurant_dict(ORG_A)

    async def _mock_get_restaurant():
        # staff_webauthn._MODULE_DEPS gates on require_module("staff_tips")
        return {**restaurant_a, "features": {"staff_tips": True}}

    app.dependency_overrides[get_current_restaurant] = _mock_get_restaurant

    # Stub verify_token so auth passes without Redis/DB
    async def _mock_verify_token(token):
        return "test_owner"

    monkeypatch.setattr("app.routes.deps.verify_token", _mock_verify_token)

    yield

    app.dependency_overrides.pop(get_current_restaurant, None)


# ── Test 1: same org → 200 ────────────────────────────────────────────────────

def test_webauthn_delete_same_org_allowed(client):
    """Credential belongs to ORG_A; authenticated restaurant is ORG_A → 200."""
    cred = _webauthn_cred(org_id=ORG_A)
    deleted_mock = AsyncMock(return_value=True)

    with (
        patch("app.routes.staff_webauthn.db.db_get_webauthn_credential", AsyncMock(return_value=cred)),
        patch("app.routes.staff_webauthn.db.db_delete_webauthn_credential", deleted_mock),
    ):
        resp = client.delete(
            "/api/staff/webauthn/credentials/base64-cred-id",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200, resp.text
    deleted_mock.assert_awaited_once_with("base64-cred-id")


# ── Test 2: cross-org → 403 ───────────────────────────────────────────────────

def test_webauthn_delete_cross_org_blocked(client):
    """Credential belongs to ORG_B; authenticated restaurant is ORG_A → 403."""
    cred = _webauthn_cred(org_id=ORG_B)

    with patch("app.routes.staff_webauthn.db.db_get_webauthn_credential", AsyncMock(return_value=cred)):
        resp = client.delete(
            "/api/staff/webauthn/credentials/base64-cred-id",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 403, resp.text


# ── Test 3: missing org_id → 403 (fail closed) ───────────────────────────────

def test_webauthn_delete_missing_org_id_fails_closed(client):
    """Credential dict has no org_id key → should fail closed with 403."""
    cred_no_org = {
        "id": "cred-uuid-1",
        "staff_id": "staff-uuid-1",
        "credential_id": "base64-cred-id",
        "public_key": "pk",
        "sign_count": 0,
        "transports": [],
        "staff_name": "Test Staff",
        # org_id intentionally absent
    }

    with patch("app.routes.staff_webauthn.db.db_get_webauthn_credential", AsyncMock(return_value=cred_no_org)):
        resp = client.delete(
            "/api/staff/webauthn/credentials/base64-cred-id",
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 403, resp.text


# ─── team_routes fixtures ─────────────────────────────────────────────────────

@pytest.fixture
def team_client(monkeypatch, client):
    """Patch get_current_user as called directly in team_routes (not FastAPI dep)."""
    user_dict = {
        "username": "owner1",
        "branch_id": LOCATION_A,   # location_id in Wave-2 context
        "role": "owner",
    }

    async def _mock_get_current_user(request):
        return user_dict

    monkeypatch.setattr("app.routes.team_routes.get_current_user", _mock_get_current_user)
    yield client


# ── Test 4: delete_branch cross-tenant → 404 ─────────────────────────────────

def test_team_delete_branch_cross_tenant_blocked(team_client):
    """Branch belongs to ORG_B; authenticated user resolves to ORG_A → 404."""
    # User's location → resolves to org_id=ORG_A
    my_rest = {"id": LOCATION_A, "org_id": ORG_A, "location_id": LOCATION_A}
    # Target branch belongs to ORG_B
    branch_row = {"id": 55, "org_id": ORG_B, "location_id": 55}

    async def _mock_get_by_id(rid):
        if rid == LOCATION_A:
            return my_rest
        if rid == 55:
            return branch_row
        return None

    with patch("app.routes.team_routes.db.db_get_restaurant_by_id", side_effect=_mock_get_by_id):
        resp = team_client.delete(
            "/api/team/branches/55",
            headers={"Authorization": "Bearer test"},
        )

    # Expect 404 (IDOR protection returns not-found, not forbidden)
    assert resp.status_code == 404, resp.text


# ── Test 5: list_team_users cross-tenant → 403 ───────────────────────────────

def test_team_list_users_cross_tenant_blocked(team_client):
    """Requesting branch 77 (ORG_B) from an ORG_A user → 403."""
    my_rest = {"id": LOCATION_A, "org_id": ORG_A, "location_id": LOCATION_A}
    foreign_branch = {"id": 77, "org_id": ORG_B, "location_id": 77}

    async def _mock_get_by_id(rid):
        if rid == LOCATION_A:
            return my_rest
        if rid == 77:
            return foreign_branch
        return None

    with patch("app.routes.team_routes.db.db_get_restaurant_by_id", side_effect=_mock_get_by_id):
        resp = team_client.get(
            "/api/team/users",
            headers={"Authorization": "Bearer test", "X-Branch-ID": "77"},
        )

    assert resp.status_code == 403, resp.text
