"""
tests/test_billing_subscription_routes.py
==========================================
Integration + unit tests for billing_subscription.py endpoints.

Repo-level tests (those touching real DB) require TEST_DATABASE_URL.
Auth-guard and validation tests use mocked auth/repo and run without a DB.

Test matrix
-----------
  test_get_plans_no_auth_required       — GET /api/billing/plans returns 4 plans + 7 addons (no auth)
  test_get_plans_shape                  — response includes 'plans' and 'addons' keys
  test_get_plan_requires_auth           — GET /api/billing/plan returns 401 without token
  test_get_usage_requires_auth          — GET /api/billing/usage returns 401 without token
  test_auto_recharge_rejects_max_6      — POST /api/billing/auto-recharge 422 for max_packs=6
  test_auto_recharge_accepts_valid      — POST /api/billing/auto-recharge 200 for valid payload
  test_buy_pack_creates_row             — POST /api/billing/buy-pack creates a pack row
  test_cap_status_shape                 — GET /api/billing/usage returns required shape keys
"""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import database as db

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

# ── Client fixture ────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(app)


# ── Auth patch helper ─────────────────────────────────────────────────────────

def _patch_auth(monkeypatch, org_id: int = 42):
    """Patch auth deps so any Bearer token is accepted and returns a mock restaurant."""
    restaurant = {
        "id":              org_id,
        "org_id":          org_id,
        "location_id":     org_id,
        "name":            "Test Restaurant",
        "whatsapp_number": "+573001234567",
        "features":        {},
    }
    user = {
        "username":        "owner_test",
        "restaurant_name": "Test Restaurant",
        "branch_id":       org_id,
        "role":            "owner",
        "password_hash":   "$2b$12$placeholder",
    }
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id", AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_check_module", AsyncMock(return_value=False))
    return restaurant


# ── Public endpoint tests (no auth) ──────────────────────────────────────────


def test_get_plans_no_auth_required(client):
    """GET /api/billing/plans must succeed without authentication."""
    # Mock the repo calls so we don't need a real DB for this test
    mock_plans = [
        {"plan_code": "pulso",       "monthly_price_cop": 149000, "sort_order": 1},
        {"plan_code": "restaurante", "monthly_price_cop": 299000, "sort_order": 2},
        {"plan_code": "pro",         "monthly_price_cop": 549000, "sort_order": 3},
        {"plan_code": "cadena",      "monthly_price_cop": 899000, "sort_order": 4},
    ]
    mock_addons = [
        {"module_code": "extra_location",      "monthly_price_cop": 199000, "sort_order": 1},
        {"module_code": "dian",                "monthly_price_cop":  99000, "sort_order": 2},
        {"module_code": "payroll",             "monthly_price_cop": 129000, "sort_order": 3},
        {"module_code": "loyalty",             "monthly_price_cop":  79000, "sort_order": 4},
        {"module_code": "inventory",           "monthly_price_cop":  79000, "sort_order": 5},
        {"module_code": "reservations_deposit","monthly_price_cop":  59000, "sort_order": 6},
        {"module_code": "marketing_extra",     "monthly_price_cop":  99000, "sort_order": 7},
    ]

    with patch(
        "app.repositories.plan_limits_repo.db_list_plans",
        AsyncMock(return_value=mock_plans),
    ), patch(
        "app.repositories.plan_limits_repo.db_list_addons",
        AsyncMock(return_value=mock_addons),
    ):
        response = client.get("/api/billing/plans")

    assert response.status_code == 200, response.text
    data = response.json()
    assert "plans" in data
    assert "addons" in data
    assert len(data["plans"]) == 4
    assert len(data["addons"]) == 7

    plan_codes = {p["plan_code"] for p in data["plans"]}
    assert plan_codes == {"pulso", "restaurante", "pro", "cadena"}

    addon_codes = {a["module_code"] for a in data["addons"]}
    assert "extra_location" in addon_codes
    assert "dian" in addon_codes
    assert "marketing_extra" in addon_codes


def test_get_plans_shape(client):
    """Response from /api/billing/plans always includes 'plans' and 'addons' keys."""
    with patch(
        "app.repositories.plan_limits_repo.db_list_plans", AsyncMock(return_value=[])
    ), patch(
        "app.repositories.plan_limits_repo.db_list_addons", AsyncMock(return_value=[])
    ):
        response = client.get("/api/billing/plans")

    assert response.status_code == 200
    data = response.json()
    assert "plans" in data
    assert "addons" in data
    assert isinstance(data["plans"], list)
    assert isinstance(data["addons"], list)


# ── Auth guard tests ──────────────────────────────────────────────────────────


def test_get_plan_requires_auth(client, monkeypatch):
    """GET /api/billing/plan returns 401 when the token is invalid."""
    # Patch verify_token to return None (invalid/missing token)
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))
    response = client.get("/api/billing/plan", headers={"Authorization": "Bearer bad-token"})
    assert response.status_code == 401, f"Expected 401, got {response.status_code}: {response.text}"


def test_get_usage_requires_auth(client, monkeypatch):
    """GET /api/billing/usage returns 401 when the token is invalid."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))
    response = client.get("/api/billing/usage", headers={"Authorization": "Bearer bad-token"})
    assert response.status_code == 401, f"Expected 401, got {response.status_code}: {response.text}"


# ── Validation tests ──────────────────────────────────────────────────────────


def test_auto_recharge_rejects_max_6(client, monkeypatch):
    """POST /api/billing/auto-recharge returns 422 when max_packs=6."""
    _patch_auth(monkeypatch, org_id=99)

    with patch(
        "app.services.tenant_context.tenant_scope",
    ) as mock_scope:
        mock_scope.return_value.__enter__ = lambda s: s
        mock_scope.return_value.__exit__ = lambda s, *a: None

        response = client.post(
            "/api/billing/auto-recharge",
            json={"enabled": True, "max_packs": 6},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 422, f"Expected 422, got {response.status_code}: {response.text}"
    detail = response.json().get("detail", "")
    assert "max_packs" in detail.lower() or "5" in detail


def test_auto_recharge_accepts_valid(client, monkeypatch):
    """POST /api/billing/auto-recharge returns 200 for a valid payload (max_packs=3)."""
    _patch_auth(monkeypatch, org_id=99)

    with patch(
        "app.repositories.plan_limits_repo.db_set_auto_recharge",
        AsyncMock(return_value=None),
    ):
        response = client.post(
            "/api/billing/auto-recharge",
            json={"enabled": True, "max_packs": 3},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["success"] is True
    assert data["auto_recharge"]["enabled"] is True
    assert data["auto_recharge"]["max_packs"] == 3


# ── Buy-pack test ─────────────────────────────────────────────────────────────


def test_buy_pack_creates_row(client, monkeypatch):
    """POST /api/billing/buy-pack creates a pack row and returns success + pack_id."""
    _patch_auth(monkeypatch, org_id=77)

    mock_sub = {
        "auto_recharge_max_packs_per_month": 5,
        "plan_code": "restaurante",
    }

    with patch(
        "app.repositories.plan_limits_repo.db_get_org_subscription",
        AsyncMock(return_value=mock_sub),
    ), patch(
        "app.repositories.plan_limits_repo.db_count_packs_this_period",
        AsyncMock(return_value=0),
    ), patch(
        "app.repositories.plan_limits_repo.db_create_pack",
        AsyncMock(return_value=1001),
    ):
        response = client.post(
            "/api/billing/buy-pack",
            json={},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data["success"] is True
    assert data["pack_id"] == 1001
    assert data["credits"] == 100
    assert data["amount_paid_cop"] == 50_000


# ── Cap status shape test ─────────────────────────────────────────────────────


def test_cap_status_shape(client, monkeypatch):
    """GET /api/billing/usage returns the expected shape with conv and audio keys."""
    _patch_auth(monkeypatch, org_id=55)

    mock_caps = {
        "comp_active": False,
        "conv": {
            "used": 10, "cap": 250, "pack_credits": 0,
            "available": 240, "pct": 0.04, "status": "ok",
        },
        "audio": {
            "used": 5.0, "cap": 60, "pct": 0.0833, "status": "ok",
        },
    }

    with patch(
        "app.repositories.plan_limits_repo.db_check_caps",
        AsyncMock(return_value=mock_caps),
    ):
        response = client.get(
            "/api/billing/usage",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert "conv" in data
    assert "audio" in data
    assert "comp_active" in data
    assert data["conv"]["status"] == "ok"
    assert data["conv"]["used"] == 10
    assert data["conv"]["cap"] == 250
    assert data["audio"]["status"] == "ok"


# ── Real DB integration tests (skipped without TEST_DATABASE_URL) ─────────────


@pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")
def test_get_plans_real_db(client):
    """GET /api/billing/plans returns seeded 4 plans from real DB (no auth)."""
    import asyncpg
    import asyncio
    from app.services import database as db_module

    # Run migration to ensure 0050 is applied (this test assumes it already ran)
    response = client.get("/api/billing/plans")
    assert response.status_code == 200
    data = response.json()

    # At minimum the seeded plans must be present
    plan_codes = {p["plan_code"] for p in data.get("plans", [])}
    assert "pulso" in plan_codes, f"'pulso' plan missing from real DB response: {plan_codes}"
    assert "cadena" in plan_codes, f"'cadena' plan missing from real DB response: {plan_codes}"
    assert len(data["plans"]) >= 4

    addon_codes = {a["module_code"] for a in data.get("addons", [])}
    assert len(data["addons"]) >= 7
    assert "dian" in addon_codes
