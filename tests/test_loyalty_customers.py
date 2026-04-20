"""
tests/test_loyalty_customers.py

Unit tests for GET /api/loyalty/customers (Sprint A — Theme 8).

Covers:
  1. Returns empty list when no loyalty customers exist
  2. Returns correct shape per customer
  3. Respects limit / offset query params
  4. Includes correct count
  5. Tenant isolation: route calls repo with authenticated restaurant org_id

No live database required — repo functions are mocked.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app


# ── Helpers ───────────────────────────────────────────────────────────────────

ORG_ID = 7
LOCATION_ID = 7


def _make_restaurant(org_id: int = ORG_ID) -> dict:
    return {
        "id": org_id,
        "org_id": org_id,
        "location_id": LOCATION_ID,
        "name": "Café Central",
        "whatsapp_number": "573001234567",
        # require_module("loyalty") reads features.get("loyalty")
        "features": {"loyalty": True},
    }


def _make_user(org_id: int = ORG_ID) -> dict:
    return {
        "username": "owner_test",
        "restaurant_name": "Café Central",
        "branch_id": org_id,
        "role": "owner",
        "password_hash": "$2b$12$placeholder",
    }


def _auth_patches(monkeypatch, org_id: int = ORG_ID):
    """Patch auth so any Bearer token resolves to the test restaurant."""
    from app.services import database as db
    restaurant = _make_restaurant(org_id)
    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=_make_user(org_id)))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))
    # Module check — loyalty enabled
    monkeypatch.setattr(db, "db_check_module", AsyncMock(return_value=True))
    return restaurant


def _make_customer_row(phone: str, points: int = 100) -> dict:
    from datetime import datetime, timezone
    return {
        "phone": phone,
        "points_balance": points,
        "total_earned": points + 50,
        "total_redeemed": 50,
        "last_activity": datetime(2026, 4, 18, 10, 0, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_loyalty_customers_empty(monkeypatch):
    """Returns count=0 and empty list when no customers exist."""
    _auth_patches(monkeypatch)
    from app.repositories import loyalty_repo

    monkeypatch.setattr(loyalty_repo, "db_get_loyalty_customers", AsyncMock(return_value=[]))
    monkeypatch.setattr(loyalty_repo, "db_count_loyalty_customers", AsyncMock(return_value=0))

    client = TestClient(app)
    resp = client.get(
        "/api/loyalty/customers",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["customers"] == []


def test_loyalty_customers_returns_correct_shape(monkeypatch):
    """Each customer entry has required fields."""
    _auth_patches(monkeypatch)
    from app.repositories import loyalty_repo

    customers = [
        {
            "phone": "573001234567",
            "points_balance": 500,
            "total_earned": 700,
            "total_redeemed": 200,
            "last_activity": "2026-04-18T10:00:00+00:00",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    monkeypatch.setattr(loyalty_repo, "db_get_loyalty_customers", AsyncMock(return_value=customers))
    monkeypatch.setattr(loyalty_repo, "db_count_loyalty_customers", AsyncMock(return_value=1))

    client = TestClient(app)
    resp = client.get(
        "/api/loyalty/customers",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert len(data["customers"]) == 1
    c = data["customers"][0]
    assert c["phone"] == "573001234567"
    assert c["points_balance"] == 500
    assert c["total_earned"] == 700
    assert c["total_redeemed"] == 200


def test_loyalty_customers_respects_limit_offset(monkeypatch):
    """Route passes limit and offset query params to the repo."""
    _auth_patches(monkeypatch)
    from app.repositories import loyalty_repo

    get_mock = AsyncMock(return_value=[])
    count_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(loyalty_repo, "db_get_loyalty_customers", get_mock)
    monkeypatch.setattr(loyalty_repo, "db_count_loyalty_customers", count_mock)

    client = TestClient(app)
    resp = client.get(
        "/api/loyalty/customers?limit=10&offset=20",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    get_mock.assert_awaited_once_with(ORG_ID, limit=10, offset=20)


def test_loyalty_customers_tenant_isolation(monkeypatch):
    """Route calls repo with the authenticated restaurant's org_id, not a different one."""
    my_org_id = 99
    _auth_patches(monkeypatch, org_id=my_org_id)
    from app.repositories import loyalty_repo

    get_mock = AsyncMock(return_value=[])
    count_mock = AsyncMock(return_value=0)
    monkeypatch.setattr(loyalty_repo, "db_get_loyalty_customers", get_mock)
    monkeypatch.setattr(loyalty_repo, "db_count_loyalty_customers", count_mock)

    client = TestClient(app)
    resp = client.get(
        "/api/loyalty/customers",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    # Both repo calls must use my_org_id
    get_mock.assert_awaited_once()
    assert get_mock.call_args[0][0] == my_org_id
    count_mock.assert_awaited_once_with(my_org_id)


def test_loyalty_customers_limit_bounds(monkeypatch):
    """limit=0 returns 422; limit=201 returns 422."""
    _auth_patches(monkeypatch)
    client = TestClient(app)

    for bad_limit in (0, 201):
        resp = client.get(
            f"/api/loyalty/customers?limit={bad_limit}",
            headers={"Authorization": "Bearer faketoken"},
        )
        assert resp.status_code == 422, f"Expected 422 for limit={bad_limit}"


def test_loyalty_customers_count_matches(monkeypatch):
    """count field reflects the total count, not just the page size."""
    _auth_patches(monkeypatch)
    from app.repositories import loyalty_repo

    # 3 customers in DB, but only 2 returned in this page
    page = [
        {"phone": "111", "points_balance": 10, "total_earned": 10, "total_redeemed": 0,
         "last_activity": None, "created_at": None},
        {"phone": "222", "points_balance": 5, "total_earned": 5, "total_redeemed": 0,
         "last_activity": None, "created_at": None},
    ]
    monkeypatch.setattr(loyalty_repo, "db_get_loyalty_customers", AsyncMock(return_value=page))
    monkeypatch.setattr(loyalty_repo, "db_count_loyalty_customers", AsyncMock(return_value=3))

    client = TestClient(app)
    resp = client.get(
        "/api/loyalty/customers?limit=2&offset=0",
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3          # total in DB
    assert len(data["customers"]) == 2  # page size
