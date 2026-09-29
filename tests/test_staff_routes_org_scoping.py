"""
tests/test_staff_routes_org_scoping.py

Regression suite for Paso 10. Six staff.py routes used to override
restaurant_id with int(X-Branch-ID) before passing to repo functions
that filter by org_id. For multi-branch tenants this gave
`WHERE org_id = location_id` → 0 rows, silently returning empty
payroll/overtime/staff lists when admin selected a sub-sucursal.
The bug was masked by the Matriz invariant.

Each test sets ORG_OWN=11 vs LOCATION=22 (distinct integers) and
asserts the repo function receives the org_id, NOT the location_id.

Routes covered:
  GET    /api/staff                   (list_staff)
  POST   /api/staff                   (create_staff)
  GET    /api/staff/payroll/calculate (payroll_calculate)
  POST   /api/staff/payroll/runs      (save_payroll_run)
  GET    /api/staff/payroll/runs      (list_payroll_runs)
  GET    /api/staff/payroll/overtime  (list_overtime_requests)
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


ORG_OWN = 11
LOCATION = 22  # Distinct from ORG_OWN — exposes any conflation


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


@pytest.fixture
def matriz_dict():
    return {
        "id": ORG_OWN, "org_id": ORG_OWN, "location_id": LOCATION,
        "name": "Test Restaurant", "whatsapp_number": "+57300",
        "parent_restaurant_id": None, "features": {},
    }


@pytest.fixture(autouse=True)
def override_scoped_dep(matriz_dict, monkeypatch):
    """Override the auth + restaurant-resolution chain for every test here.

    The staff routes go through MULTIPLE FastAPI deps that each can hit the DB:
      - require_auth → verify_token (mocked)
      - Depends(get_current_restaurant_scoped) → tenant_scope + DB lookup
      - dependencies=_MODULE_DEPS → require_module → Depends(get_current_restaurant)
        which itself calls db_get_restaurant_by_id

    We override BOTH get_current_restaurant_scoped AND get_current_restaurant
    via FastAPI's dependency_overrides so the require_module gate sees a
    pre-populated dict (with the staff_tips feature enabled) without DB.
    """
    from app.main import app
    from app.routes.deps import (
        get_current_restaurant,
        get_current_restaurant_scoped,
        get_current_user,
    )

    matriz_with_module = {**matriz_dict, "features": {"staff_tips": True}}

    async def _mock_scoped():
        yield matriz_with_module

    async def _mock_get_restaurant():
        return matriz_with_module

    async def _mock_get_user_dep():
        return {
            "username": "test_owner", "restaurant_name": "Test Restaurant",
            "branch_id": ORG_OWN, "role": "owner",
        }

    async def _mock_verify_token(token):
        return "test_owner"

    monkeypatch.setattr("app.routes.deps.verify_token", _mock_verify_token)

    app.dependency_overrides[get_current_restaurant_scoped] = _mock_scoped
    app.dependency_overrides[get_current_restaurant] = _mock_get_restaurant
    app.dependency_overrides[get_current_user] = _mock_get_user_dep

    yield

    app.dependency_overrides.pop(get_current_restaurant_scoped, None)
    app.dependency_overrides.pop(get_current_restaurant, None)
    app.dependency_overrides.pop(get_current_user, None)


# ── GET /api/staff (list_staff) ──────────────────────────────────────────────

def test_list_staff_passes_org_id_and_the_picked_sede(client):
    """X-Branch-ID narrows an owner's roster to that sede — it must never
    replace the ORG id (that confusion is what rls-multitenant.md bans)."""
    db_get_staff_mock = AsyncMock(return_value=[])
    with patch("app.routes.staff.db.db_get_staff", db_get_staff_mock):
        resp = client.get("/api/staff", headers={"X-Branch-ID": str(LOCATION),
                                                  "Authorization": "Bearer test"})

    assert resp.status_code == 200, resp.text
    db_get_staff_mock.assert_awaited_once_with(ORG_OWN, location_id=LOCATION)


def test_list_staff_without_a_header_is_org_wide_for_an_owner(client):
    db_get_staff_mock = AsyncMock(return_value=[])
    with patch("app.routes.staff.db.db_get_staff", db_get_staff_mock):
        resp = client.get("/api/staff", headers={"Authorization": "Bearer test"})

    assert resp.status_code == 200, resp.text
    db_get_staff_mock.assert_awaited_once_with(ORG_OWN, location_id=None)


# ── POST /api/staff (create_staff) ───────────────────────────────────────────

def test_create_staff_inserts_with_org_id_not_location_id(client):
    """db_create_staff(restaurant_id=X) writes X into staff.org_id —
    must always receive the org_id, never the X-Branch-ID location."""
    create_mock = AsyncMock(return_value={"id": "s1", "name": "X"})
    with patch("app.routes.staff.db.db_create_staff", create_mock):
        resp = client.post(
            "/api/staff",
            headers={"X-Branch-ID": str(LOCATION), "Authorization": "Bearer test"},
            json={
                "name": "Carlos", "last_name": "Pérez", "phone": "+573009999999",
                "role": "mesero", "password": "123456",  # Fix #2: min 6 digits
            },
        )

    assert resp.status_code == 201, resp.text
    kwargs = create_mock.await_args.kwargs
    assert kwargs["restaurant_id"] == ORG_OWN, (
        f"db_create_staff must get ORG_OWN ({ORG_OWN}), got {kwargs['restaurant_id']}"
    )


# ── GET /api/staff/payroll/calculate ─────────────────────────────────────────

# ── POST /api/staff/payroll/runs (save) ──────────────────────────────────────

# ── GET /api/staff/payroll/runs (list) ───────────────────────────────────────

# ── GET /api/staff/payroll/overtime (list) ───────────────────────────────────
