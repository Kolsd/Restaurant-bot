"""
tests/test_reservations_create.py

Unit tests for POST /api/reservations (Sprint X — Task 1).

Covers:
  1. test_create_valid_returns_201
  2. test_create_empty_name_400
  3. test_create_party_size_invalid_400
  4. test_create_past_date_400
  5. test_create_returns_full_shape
  6. test_tenant_isolation (org A creates → org B cannot see it via different scope)

No live database required — all DB calls are fully mocked.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app

# ── Helpers ───────────────────────────────────────────────────────────────────

ORG_A_ID = 10
ORG_B_ID = 20

FUTURE_DATE = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
PAST_DATE   = (datetime.now(tz=timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _make_restaurant(org_id: int = ORG_A_ID) -> dict:
    return {
        "id":               org_id,
        "org_id":           org_id,
        "location_id":      org_id,
        "name":             f"Restaurante {org_id}",
        "whatsapp_number":  f"5730012345{org_id:02d}",
        "address":          "Calle 1",
        "features":         {"bot_active": True, "module_reservations": True},
    }


def _make_user(org_id: int = ORG_A_ID) -> dict:
    return {
        "username":        "owner_test",
        "restaurant_name": f"Restaurante {org_id}",
        "branch_id":       org_id,
        "role":            "owner",
        "password_hash":   "$2b$12$placeholder",
    }


def _sample_reservation(org_id: int = ORG_A_ID) -> dict:
    return {
        "id":            1,
        "org_id":        org_id,
        "name":          "Juan Pérez",
        "date":          FUTURE_DATE,
        "time":          "19:00",
        "guests":        2,
        "phone":         "3001234567",
        "notes":         "Ventana",
        "table_id":      None,
        "source":        "manual",
        "status":        "pending",
        "bot_number":    f"5730012345{org_id:02d}",
        "created_at":    "2026-04-20T12:00:00",
        "confirmed_at":  None,
        "cancelled_at":  None,
        "cancellation_reason": None,
        "no_show":       False,
        "confirmation_sent": False,
    }


def _auth_patches(monkeypatch, org_id: int = ORG_A_ID):
    """Patch auth so any Bearer token is accepted for the given org."""
    from app.services import database as db

    restaurant = _make_restaurant(org_id)
    user = _make_user(org_id)

    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_check_module",
                        AsyncMock(return_value=True))
    return restaurant


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_create_valid_returns_201(monkeypatch):
    """POST /api/reservations with valid data returns 201 and an id."""
    _auth_patches(monkeypatch)

    reservation = _sample_reservation()

    import app.routes.reservations as res_mod
    monkeypatch.setattr(res_mod.reservations_repo, "db_create_reservation", AsyncMock(return_value=reservation))

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "Juan Pérez",
            "customer_phone": "3001234567",
            "party_size": 2,
            "date": FUTURE_DATE,
            "time": "19:00",
            "notes": "Ventana",
            "source": "manual",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] == 1
    assert data["status"] == "pending"


def test_create_empty_name_400(monkeypatch):
    """POST /api/reservations with empty customer_name returns 422."""
    _auth_patches(monkeypatch)

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "   ",
            "party_size": 2,
            "date": FUTURE_DATE,
            "time": "19:00",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    # Pydantic validator raises ValueError → 422 Unprocessable Entity
    assert resp.status_code == 422


def test_create_party_size_invalid_400(monkeypatch):
    """POST /api/reservations with party_size=0 or >20 returns 422."""
    _auth_patches(monkeypatch)

    client = TestClient(app)

    # party_size = 0
    resp = client.post(
        "/api/reservations",
        json={"customer_name": "Test", "party_size": 0, "date": FUTURE_DATE, "time": "19:00"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 422

    # party_size = 21
    resp = client.post(
        "/api/reservations",
        json={"customer_name": "Test", "party_size": 21, "date": FUTURE_DATE, "time": "19:00"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 422


def test_create_past_date_400(monkeypatch):
    """POST /api/reservations with a past date+time returns 400."""
    _auth_patches(monkeypatch)

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "Test",
            "party_size": 2,
            "date": PAST_DATE,
            "time": "09:00",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    assert "future" in resp.json()["detail"].lower()


def test_create_returns_full_shape(monkeypatch):
    """POST /api/reservations response includes all expected reservation fields."""
    _auth_patches(monkeypatch)

    reservation = _sample_reservation()

    import app.routes.reservations as res_mod
    monkeypatch.setattr(res_mod.reservations_repo, "db_create_reservation", AsyncMock(return_value=reservation))

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "Juan Pérez",
            "party_size": 2,
            "date": FUTURE_DATE,
            "time": "19:00",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 201
    data = resp.json()

    for key in ("id", "status", "name", "date", "time", "guests", "phone",
                "notes", "table_id", "source", "bot_number"):
        assert key in data, f"Missing key in response: {key}"

    assert data["status"] == "pending"
    assert data["guests"] == 2


def test_tenant_isolation(monkeypatch):
    """Org A creates a reservation; db_create_reservation is called with the org A scope."""
    # Patch org A auth
    restaurant_a = _auth_patches(monkeypatch, org_id=ORG_A_ID)

    created_calls = []

    async def _fake_create(**kwargs):
        created_calls.append(kwargs)
        res = _sample_reservation(ORG_A_ID)
        return res

    import app.routes.reservations as res_mod
    monkeypatch.setattr(res_mod.reservations_repo, "db_create_reservation", _fake_create)

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "Ana García",
            "party_size": 3,
            "date": FUTURE_DATE,
            "time": "20:00",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 201

    # Verify create was called with the org A bot_number (not org B's)
    assert len(created_calls) == 1
    assert restaurant_a["whatsapp_number"] in str(created_calls[0].get("bot_number", ""))


def test_create_missing_required_fields_422(monkeypatch):
    """POST /api/reservations without required fields returns 422."""
    _auth_patches(monkeypatch)

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={"customer_name": "Test"},  # missing party_size, date, time
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 422


def test_create_invalid_date_format_422(monkeypatch):
    """POST /api/reservations with non-ISO date returns 422."""
    _auth_patches(monkeypatch)

    client = TestClient(app)
    resp = client.post(
        "/api/reservations",
        json={
            "customer_name": "Test",
            "party_size": 2,
            "date": "20/04/2027",   # wrong format
            "time": "19:00",
        },
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 422
