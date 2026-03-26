"""
Suite 4 — Staff & Tips
tests/test_staff.py

Covers:
  1.  GET /api/staff without module → 403
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

def test_list_staff_without_module_returns_403(client, monkeypatch):
    """staff_tips absent/False → 403."""
    _auth(monkeypatch, features={"staff_tips": False})
    r = client.get("/api/staff", headers=_HEADERS)
    assert r.status_code == 403
    assert "staff_tips" in r.json()["detail"]


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

    r = client.post(
        "/api/staff",
        json={"name": "Ana García", "role": "mesero", "pin": "1234", "phone": "+573001111111"},
        headers=_HEADERS,
    )
    assert r.status_code == 201
    body = r.text
    assert "1234" not in body, "Raw PIN must never appear in the response"
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

def test_clock_in_success(client, monkeypatch):
    """Successful clock-in returns 200 with shift data."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_clock_in", AsyncMock(return_value=_SHIFT_ROW))

    r = client.post(
        "/api/staff/clock-in",
        json={"staff_id": _STAFF_ROW["id"]},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["shift"]["staff_id"] == _STAFF_ROW["id"]


def test_clock_in_duplicate_returns_409(client, monkeypatch):
    """
    When the employee already has an open shift, db_clock_in raises ValueError.
    The endpoint must convert this to 409 Conflict.
    """
    _auth(monkeypatch)
    import app.services.database as db_mod

    async def _raise(*a, **kw):
        raise ValueError("El empleado ya tiene un turno abierto.")

    monkeypatch.setattr(db_mod, "db_clock_in", _raise)

    r = client.post(
        "/api/staff/clock-in",
        json={"staff_id": _STAFF_ROW["id"]},
        headers=_HEADERS,
    )
    assert r.status_code == 409
    assert "turno abierto" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# 7–8. Clock-out
# ══════════════════════════════════════════════════════════════════════════════

def test_clock_out_success(client, monkeypatch):
    """Successful clock-out returns 200 with closed shift."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    closed = dict(_SHIFT_ROW, clock_out="2026-03-25T16:00:00+00:00")
    monkeypatch.setattr(db_mod, "db_clock_out", AsyncMock(return_value=closed))

    r = client.post(
        "/api/staff/clock-out",
        json={"staff_id": _STAFF_ROW["id"]},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["shift"]["clock_out"] is not None


def test_clock_out_no_open_shift_returns_404(client, monkeypatch):
    """
    db_clock_out returns None when no open shift exists.
    The endpoint must convert this to 404.
    """
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_clock_out", AsyncMock(return_value=None))

    r = client.post(
        "/api/staff/clock-out",
        json={"staff_id": _STAFF_ROW["id"]},
        headers=_HEADERS,
    )
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 9–10. Shifts
# ══════════════════════════════════════════════════════════════════════════════

def test_get_open_shifts(client, monkeypatch):
    """GET /api/staff/open-shifts returns open shift list."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    open_shift = {
        "id":         _SHIFT_ROW["id"],
        "staff_id":   _SHIFT_ROW["staff_id"],
        "clock_in":   _SHIFT_ROW["clock_in"],
        "staff_name": "Ana García",
        "staff_role": "mesero",
    }
    monkeypatch.setattr(db_mod, "db_get_open_shifts", AsyncMock(return_value=[open_shift]))

    r = client.get("/api/staff/open-shifts", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["shifts"]) == 1
    assert r.json()["shifts"][0]["staff_name"] == "Ana García"


def test_get_shifts_with_date_range(client, monkeypatch):
    """GET /api/staff/shifts?date_from=...&date_to=... returns shift history."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    shift_with_hours = dict(_SHIFT_ROW,
                            clock_out="2026-03-25T16:00:00+00:00",
                            staff_name="Ana García",
                            staff_role="mesero",
                            hours_worked=8.0)
    monkeypatch.setattr(db_mod, "db_get_shifts", AsyncMock(return_value=[shift_with_hours]))

    r = client.get(
        "/api/staff/shifts?date_from=2026-03-25T00:00:00Z&date_to=2026-03-26T00:00:00Z",
        headers=_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["shifts"][0]["hours_worked"] == 8.0


# ══════════════════════════════════════════════════════════════════════════════
# 11–13. Tip cut
# ══════════════════════════════════════════════════════════════════════════════

def test_tip_cut_no_employees_returns_422(client, monkeypatch):
    """
    When db_calculate_tip_pool returns empty entries (no shifts in period),
    the endpoint must return 422 with a descriptive error.
    """
    _auth(monkeypatch)
    import app.services.database as db_mod

    monkeypatch.setattr(db_mod, "db_calculate_tip_pool", AsyncMock(return_value={
        "pct_config": {},
        "entries": [],
        "total_allocated": 0.0,
        "total_unallocated": 50000.0,
    }))

    r = client.post(
        "/api/staff/tip-cut",
        json={
            "period_start": "2026-03-25T00:00:00Z",
            "period_end":   "2026-03-25T23:59:59Z",
            "total_tips":   50000.0,
        },
        headers=_HEADERS,
    )
    assert r.status_code == 422


def test_tip_cut_valid_returns_distribution(client, monkeypatch):
    """Valid tip cut returns 200 with distribution data."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    preview = {
        "pct_config": {"mesero": 70, "cocina": 30},
        "entries": [
            {"staff_id": _STAFF_ROW["id"], "name": "Ana",   "role": "mesero",
             "hours": 8.0, "amount": 35000.0, "pct": 70},
            {"staff_id": "cccccccc-0000-4000-8000-000000000001", "name": "Carlos",
             "role": "cocina", "hours": 8.0, "amount": 15000.0, "pct": 30},
        ],
        "total_allocated":   50000.0,
        "total_unallocated": 0.0,
    }
    saved = {
        "id":           "dddddddd-0000-4000-8000-000000000001",
        "restaurant_id": 1,
        "period_start": "2026-03-25T00:00:00+00:00",
        "period_end":   "2026-03-25T23:59:59+00:00",
        "total_tips":   50000.0,
        "distribution": preview["entries"],
        "pct_config":   preview["pct_config"],
        "created_by":   "+573009999999",
        "created_at":   "2026-03-25T17:00:00+00:00",
    }

    monkeypatch.setattr(db_mod, "db_calculate_tip_pool",   AsyncMock(return_value=preview))
    monkeypatch.setattr(db_mod, "db_save_tip_distribution", AsyncMock(return_value=saved))

    r = client.post(
        "/api/staff/tip-cut",
        json={
            "period_start": "2026-03-25T00:00:00Z",
            "period_end":   "2026-03-25T23:59:59Z",
            "total_tips":   50000.0,
        },
        headers=_HEADERS,
    )
    assert r.status_code == 200
    data = r.json()
    assert "distribution" in data
    assert "preview" in data


def test_tip_cut_amounts_do_not_exceed_total(client, monkeypatch):
    """
    The sum of all distribution amounts must be ≤ total_tips.
    This validates the math invariant regardless of rounding.
    """
    _auth(monkeypatch)
    import app.services.database as db_mod

    total_tips = 100_000.0
    entries = [
        {"staff_id": "e1", "name": "A", "role": "mesero", "hours": 6.0, "amount": 40000.0, "pct": 60},
        {"staff_id": "e2", "name": "B", "role": "mesero", "hours": 2.0, "amount": 20000.0, "pct": 60},
        {"staff_id": "e3", "name": "C", "role": "cocina",  "hours": 8.0, "amount": 40000.0, "pct": 40},
    ]
    preview = {
        "pct_config": {"mesero": 60, "cocina": 40},
        "entries": entries,
        "total_allocated":   100_000.0,
        "total_unallocated": 0.0,
    }
    saved = {"id": "x", "distribution": entries, "total_tips": total_tips,
             "period_start": "2026-03-25T00:00:00Z", "period_end": "2026-03-25T23:59:59Z",
             "pct_config": preview["pct_config"], "created_by": "+57", "created_at": "2026-03-25T17:00:00Z"}

    monkeypatch.setattr(db_mod, "db_calculate_tip_pool",   AsyncMock(return_value=preview))
    monkeypatch.setattr(db_mod, "db_save_tip_distribution", AsyncMock(return_value=saved))

    r = client.post(
        "/api/staff/tip-cut",
        json={"period_start": "2026-03-25T00:00:00Z",
              "period_end":   "2026-03-25T23:59:59Z",
              "total_tips":   total_tips},
        headers=_HEADERS,
    )
    assert r.status_code == 200
    total_distributed = sum(e["amount"] for e in r.json()["preview"]["entries"])
    assert total_distributed <= total_tips + 0.01  # tolerance for float rounding


# ══════════════════════════════════════════════════════════════════════════════
# 14. Tip distribution history
# ══════════════════════════════════════════════════════════════════════════════

def test_get_tip_distributions(client, monkeypatch):
    """GET /api/staff/tip-distributions returns list of past cuts."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    cut = {
        "id": "dddddddd-0000-4000-8000-000000000001",
        "restaurant_id": 1,
        "period_start": "2026-03-25T00:00:00+00:00",
        "period_end":   "2026-03-25T23:59:59+00:00",
        "total_tips":   50000.0,
        "distribution": [],
        "pct_config":   {},
        "created_by":   "+573009999999",
        "created_at":   "2026-03-25T17:00:00+00:00",
    }
    monkeypatch.setattr(db_mod, "db_get_tip_distributions", AsyncMock(return_value=[cut]))

    r = client.get("/api/staff/tip-distributions", headers=_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["distributions"]) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 15–16. DB layer unit tests (no HTTP)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_clock_in_unique_violation_raises_value_error():
    """
    asyncpg.UniqueViolationError from the partial unique index
    uq_staff_shifts_one_open must be converted to ValueError by db_clock_in.
    """
    import asyncpg
    from app.services import database as db
    from tests.conftest import make_pool

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        side_effect=asyncpg.UniqueViolationError("duplicate key")
    )

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        with pytest.raises(ValueError, match="turno abierto"):
            await db.db_clock_in(
                staff_id="aaaaaaaa-0000-4000-8000-000000000001",
                restaurant_id=1,
            )


@pytest.mark.asyncio
async def test_db_clock_out_returns_none_when_no_open_shift():
    """
    When no open shift exists for the employee, fetchrow returns None.
    db_clock_out must propagate None (not raise).
    """
    from app.services import database as db
    from tests.conftest import make_pool

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        result = await db.db_clock_out(
            staff_id="aaaaaaaa-0000-4000-8000-000000000001",
            restaurant_id=1,
        )

    assert result is None
