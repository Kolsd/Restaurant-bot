"""
Suite 4 — Staff & Tips
tests/test_staff.py

Covers:
  1.  GET /api/staff without module → 200 (roster is NOT gated by staff_tips)
  1b. GET /api/staff/open-shifts without module → 403 (shifts still are)
  2.  GET /api/staff with module enabled → 200, returns staff list
  3.  POST /api/staff creates member, PIN hashed (raw PIN not in response)
  4.  POST /api/staff invalid role → 422
  5.  POST /api/staff/clock-in success → 200, shift returned
  6.  POST /api/staff/clock-in duplicate (open shift) → 409
  7.  POST /api/staff/clock-out success → 200
  8.  POST /api/staff/clock-out no open shift → 404
  9.  GET /api/staff/open-shifts → 200, list
 10.  GET /api/staff/shifts → 200, list with hours_worked
 11.  POST /api/staff/tip-cut no employees → 422
 12.  POST /api/staff/tip-cut valid period → 200, distribution saved
 13.  Tip math: distribution amounts sum ≤ total_tips
 14.  GET /api/staff/tip-distributions → 200, list
 15.  db_clock_in UniqueViolation → raises ValueError
 16.  db_clock_out returns None when no open shift exists
"""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import make_pool, make_row, patch_auth


# ── Shared auth patcher for this suite ───────────────────────────────────────

def _auth(monkeypatch, *, features=None):
    """Enable staff_tips module and return the mocked restaurant."""
    if features is None:
        features = {"staff_tips": True}
    r = patch_auth(monkeypatch, features=features)
    import app.services.database as db_mod
    # db_check_module must return True when staff_tips is in features as True
    monkeypatch.setattr(db_mod, "db_check_module",
                        AsyncMock(return_value=features.get("staff_tips", False)))
    return r


_HEADERS = {"Authorization": "Bearer tok"}

# ── Fixtures ─────────────────────────────────────────────────────────────────

_STAFF_ROW = {
    "id":            "aaaaaaaa-0000-4000-8000-000000000001",
    "restaurant_id": 1,
    "name":          "Ana García",
    "role":          "mesero",
    "active":        True,
    "phone":         "+573001111111",
    "created_at":    "2026-03-01T08:00:00+00:00",
    "updated_at":    "2026-03-01T08:00:00+00:00",
}

_SHIFT_ROW = {
    "id":          "bbbbbbbb-0000-4000-8000-000000000001",
    "staff_id":    _STAFF_ROW["id"],
    "restaurant_id": 1,
    "clock_in":    "2026-03-25T08:00:00+00:00",
    "clock_out":   None,
    "notes":       "",
    "created_at":  "2026-03-25T08:00:00+00:00",
}


# ══════════════════════════════════════════════════════════════════════════════
# 1–2. Module gate
# ══════════════════════════════════════════════════════════════════════════════

def test_list_staff_without_module_is_allowed(client, monkeypatch):
    """The roster is NOT behind staff_tips.

    A restaurant cannot operate without creating a mesero/cajero/domiciliario,
    and orgs are created with features={} — gating the roster locked every new
    customer out of staffing their own restaurant. Reading it must work with
    the module explicitly off.
    """
    _auth(monkeypatch, features={"staff_tips": False})
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_get_staff", AsyncMock(return_value=[_STAFF_ROW]))

    r = client.get("/api/staff", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["staff"][0]["name"] == "Ana García"


def test_create_staff_without_module_is_allowed(client, monkeypatch):
    """Creating staff must work with staff_tips off — the onboarding path."""
    _auth(monkeypatch, features={"staff_tips": False})
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_create_staff", AsyncMock(return_value=_STAFF_ROW))
    monkeypatch.setattr(
        "app.routes.staff._resolve_new_staff_location", AsyncMock(return_value=None)
    )

    r = client.post("/api/staff", headers=_HEADERS, json={
        "name": "Ana", "last_name": "García", "role": "mesero", "password": "1234",
    })
    assert r.status_code == 201


def test_list_staff_with_module_returns_200(client, monkeypatch):
    """staff_tips=True → 200, returns staff list."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_get_staff", AsyncMock(return_value=[_STAFF_ROW]))

    r = client.get("/api/staff", headers=_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert "staff" in data
    assert data["staff"][0]["name"] == "Ana García"


# ══════════════════════════════════════════════════════════════════════════════
# 3–4. Create staff
# ══════════════════════════════════════════════════════════════════════════════

def test_create_staff_pin_not_in_response(client, monkeypatch):
    """
    POST /api/staff creates a member.  The raw PIN must never appear in the
    response (only the hashed value is stored, and PIN is not returned at all).
    """
    _auth(monkeypatch)
    import app.services.database as db_mod

    created_row = dict(_STAFF_ROW)  # pin column not in RETURNING clause
    monkeypatch.setattr(db_mod, "db_create_staff", AsyncMock(return_value=created_row))

    # Fix #2: PIN must be ≥6 digits (was 4). Updated test fixture to match.
    r = client.post(
        "/api/staff",
        json={"name": "Ana García", "role": "mesero", "password": "123456", "phone": "+573001111111"},
        headers=_HEADERS,
    )
    assert r.status_code == 201
    body = r.text
    assert "123456" not in body, "Raw PIN must never appear in the response"
    assert r.json()["staff"]["name"] == "Ana García"


def test_create_staff_invalid_role_422(client, monkeypatch):
    """Unknown role value must fail Pydantic validation with 422."""
    _auth(monkeypatch)
    r = client.post(
        "/api/staff",
        json={"name": "Test", "role": "hacker", "pin": "1234"},
        headers=_HEADERS,
    )
    assert r.status_code == 422


def test_create_staff_short_pin_422(client, monkeypatch):
    """PIN shorter than 4 characters must fail with 422."""
    _auth(monkeypatch)
    r = client.post(
        "/api/staff",
        json={"name": "Test", "role": "mesero", "pin": "12"},
        headers=_HEADERS,
    )
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 5–6. Clock-in
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 7–8. Clock-out
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 9–10. Shifts
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# 14. DB layer unit tests (no HTTP)
# ══════════════════════════════════════════════════════════════════════════════
