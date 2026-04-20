"""
tests/test_settings_expansion.py

Unit tests for the expanded POST /api/settings handler (Sprint A — Theme 4).

Covers:
  1. GET /api/settings returns nit, city, cuisine_type fields from features
  2. POST saves name via db_update_location
  3. POST saves address via db_update_location
  4. POST saves opening_hours via db_update_location
  5. POST saves nit, city, cuisine_type, notifications via db_merge_restaurant_features
  6. POST rejects empty name (400)
  7. POST returns updated settings shape including all new fields
  8. Tenant isolation: restaurant is loaded from the authenticated user's branch_id

No live database required — DB calls are fully mocked.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from app.main import app

# ── Helpers ───────────────────────────────────────────────────────────────────

RESTAURANT_ID = 42


def _make_restaurant(extra_features: dict | None = None) -> dict:
    features = {
        "bot_active": True,
        "payment_methods": ["efectivo"],
        "timezone": "America/Bogota",
        "currency": "COP",
        "locale": "es-CO",
    }
    if extra_features:
        features.update(extra_features)
    return {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10 # 5-30",
        "latitude": 4.711,
        "longitude": -74.072,
        "features": features,
    }


def _make_user() -> dict:
    return {
        "username": "owner_test",
        "restaurant_name": "El Fogón",
        "branch_id": RESTAURANT_ID,
        "role": "owner",
        "password_hash": "$2b$12$placeholder",
    }


def _auth_patches(monkeypatch, restaurant: dict | None = None):
    """Patch token verification and DB lookups so any Bearer token is accepted."""
    if restaurant is None:
        restaurant = _make_restaurant()
    from app.services import database as db

    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=_make_user()))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_check_module",
                        AsyncMock(return_value=False))
    return restaurant


# ── GET /api/settings ─────────────────────────────────────────────────────────

def test_get_settings_returns_new_fields(monkeypatch):
    """GET /api/settings must return nit, city, cuisine_type (from features JSONB)."""
    restaurant = _make_restaurant({
        "nit": "900123456-7",
        "city": "Bogotá",
        "cuisine_type": "colombiana",
    })
    _auth_patches(monkeypatch, restaurant)

    client = TestClient(app)
    resp = client.get("/api/settings", headers={"Authorization": "Bearer faketoken"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["nit"] == "900123456-7"
    assert data["city"] == "Bogotá"
    assert data["cuisine_type"] == "colombiana"
    assert data["name"] == "El Fogón"
    assert data["address"] == "Calle 10 # 5-30"


def test_get_settings_defaults_for_new_fields(monkeypatch):
    """GET /api/settings returns empty string defaults when features lack new fields."""
    _auth_patches(monkeypatch)
    client = TestClient(app)
    resp = client.get("/api/settings", headers={"Authorization": "Bearer faketoken"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["nit"] == ""
    assert data["city"] == ""
    assert data["cuisine_type"] == ""


# ── POST /api/settings — name ─────────────────────────────────────────────────

def test_post_settings_saves_name(monkeypatch):
    """POST /api/settings with name must call db_update_location with name."""
    restaurant = _make_restaurant()
    _auth_patches(monkeypatch, restaurant)

    updated = _make_restaurant()
    updated["name"] = "La Nueva Sede"
    from app.services import database as db
    from app.repositories import restaurant_repo
    from app.services.tenant_context import tenant_scope

    db.db_get_restaurant_by_id = AsyncMock(side_effect=[restaurant, updated])
    merge_mock = AsyncMock(return_value=updated["features"])
    loc_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)
    monkeypatch.setattr(restaurant_repo, "db_update_location", loc_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"name": "La Nueva Sede"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    # db_update_location must have been called with name kwarg
    loc_mock.assert_awaited_once()
    call_kwargs = loc_mock.call_args[1]
    assert call_kwargs.get("name") == "La Nueva Sede"


def test_post_settings_rejects_empty_name(monkeypatch):
    """POST /api/settings with empty name returns 400."""
    _auth_patches(monkeypatch)
    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"name": "   "},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400
    assert "vacío" in resp.json()["detail"].lower()


def test_post_settings_rejects_empty_string_name(monkeypatch):
    """POST /api/settings with name='' returns 400."""
    _auth_patches(monkeypatch)
    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"name": ""},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 400


# ── POST /api/settings — features fields ─────────────────────────────────────

def test_post_settings_saves_nit_city_cuisine(monkeypatch):
    """POST saves nit, city, cuisine_type via db_merge_restaurant_features."""
    restaurant = _make_restaurant()
    _auth_patches(monkeypatch, restaurant)

    from app.services import database as db
    from app.repositories import restaurant_repo

    merged_features = dict(restaurant["features"])
    merged_features.update({"nit": "900000001-5", "city": "Medellín", "cuisine_type": "criolla"})
    updated = _make_restaurant({"nit": "900000001-5", "city": "Medellín", "cuisine_type": "criolla"})

    db.db_get_restaurant_by_id = AsyncMock(side_effect=[restaurant, updated])
    merge_mock = AsyncMock(return_value=merged_features)
    loc_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)
    monkeypatch.setattr(restaurant_repo, "db_update_location", loc_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"nit": "900000001-5", "city": "Medellín", "cuisine_type": "criolla"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    merge_mock.assert_awaited_once()
    patch_arg = merge_mock.call_args[0][1]  # second positional arg = patch dict
    assert patch_arg["nit"] == "900000001-5"
    assert patch_arg["city"] == "Medellín"
    assert patch_arg["cuisine_type"] == "criolla"


def test_post_settings_saves_notifications(monkeypatch):
    """POST saves notifications dict via db_merge_restaurant_features."""
    restaurant = _make_restaurant()
    _auth_patches(monkeypatch, restaurant)

    from app.services import database as db
    from app.repositories import restaurant_repo

    notif = {"new_order": True, "low_stock": False}
    merged_features = dict(restaurant["features"])
    merged_features["notifications"] = notif
    updated = _make_restaurant({"notifications": notif})

    db.db_get_restaurant_by_id = AsyncMock(side_effect=[restaurant, updated])
    merge_mock = AsyncMock(return_value=merged_features)
    loc_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)
    monkeypatch.setattr(restaurant_repo, "db_update_location", loc_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"notifications": notif},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    patch_arg = merge_mock.call_args[0][1]
    assert patch_arg["notifications"] == notif


def test_post_settings_saves_opening_hours(monkeypatch):
    """POST saves opening_hours dict via db_update_location."""
    restaurant = _make_restaurant()
    _auth_patches(monkeypatch, restaurant)

    from app.services import database as db
    from app.repositories import restaurant_repo

    hours = {"lun": {"open": "08:00", "close": "22:00"}, "dom": {"open": "10:00", "close": "20:00"}}
    updated = _make_restaurant()

    db.db_get_restaurant_by_id = AsyncMock(side_effect=[restaurant, updated])
    merge_mock = AsyncMock(return_value=restaurant["features"])
    loc_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)
    monkeypatch.setattr(restaurant_repo, "db_update_location", loc_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"opening_hours": hours},
        headers={"Authorization": "Bearer faketoken"},
    )
    # opening_hours is a dict → goes through features_patch (updatable list includes it — no)
    # Actually opening_hours is NOT in _features_updatable, it goes via db_update_location.
    # Verify loc_mock was called with opening_hours
    assert resp.status_code == 200
    loc_mock.assert_awaited_once()
    call_kwargs = loc_mock.call_args[1]
    assert call_kwargs.get("opening_hours") == hours


# ── POST /api/settings — response shape ──────────────────────────────────────

def test_post_settings_returns_complete_shape(monkeypatch):
    """POST /api/settings response includes all settings fields (including new ones)."""
    restaurant = _make_restaurant({"nit": "800000001-2", "city": "Cali"})
    _auth_patches(monkeypatch, restaurant)

    from app.services import database as db
    from app.repositories import restaurant_repo

    db.db_get_restaurant_by_id = AsyncMock(side_effect=[restaurant, restaurant])
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features",
                        AsyncMock(return_value=restaurant["features"]))
    monkeypatch.setattr(restaurant_repo, "db_update_location", AsyncMock(return_value=None))

    client = TestClient(app)
    resp = client.post(
        "/api/settings",
        json={"currency": "COP"},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # All mandatory keys present
    for key in ("restaurant_id", "name", "address", "nit", "city", "cuisine_type",
                 "features", "timezone", "currency", "locale"):
        assert key in data, f"Missing key: {key}"
    assert data["nit"] == "800000001-2"
    assert data["city"] == "Cali"


# ── POST /api/settings/pause ──────────────────────────────────────────────────


def _pause_auth_patches(monkeypatch, role: str = "owner", features: dict | None = None):
    """Patch auth for pause endpoint tests."""
    feats = {
        "bot_active": True,
        "payment_methods": ["efectivo"],
        "timezone": "America/Bogota",
        "currency": "COP",
        "locale": "es-CO",
    }
    if features:
        feats.update(features)

    restaurant = {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10",
        "features": feats,
    }
    user = {
        "username": "owner_test",
        "restaurant_name": "El Fogón",
        "branch_id": RESTAURANT_ID,
        "role": role,
        "password_hash": "$2b$12$placeholder",
    }

    from app.services import database as db

    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id",
                        AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_check_module",
                        AsyncMock(return_value=False))
    return restaurant


def test_pause_sets_bot_active_false(monkeypatch):
    """POST /api/settings/pause with paused=true sets bot_active=false in features."""
    _pause_auth_patches(monkeypatch)

    from app.repositories import restaurant_repo

    paused_features = {
        "bot_active": False,
        "paused_at": "2026-04-20T12:00:00Z",
        "paused_by": "owner_test",
    }
    paused_restaurant = {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10",
        "features": paused_features,
    }

    from app.services import database as db
    db.db_get_restaurant_by_id = AsyncMock(side_effect=[
        _pause_auth_patches.__wrapped__(monkeypatch) if hasattr(_pause_auth_patches, "__wrapped__") else paused_restaurant,
        paused_restaurant,
    ])

    merge_mock = AsyncMock(return_value=paused_features)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/pause",
        json={"paused": True},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200, resp.text
    merge_mock.assert_awaited_once()
    patch_arg = merge_mock.call_args[0][1]
    assert patch_arg["bot_active"] is False


def test_unpause_sets_bot_active_true(monkeypatch):
    """POST /api/settings/pause with paused=false sets bot_active=true."""
    _pause_auth_patches(monkeypatch, features={"bot_active": False, "paused_at": "2026-04-20T10:00:00Z"})

    from app.repositories import restaurant_repo
    from app.services import database as db

    active_features = {"bot_active": True, "paused_at": None, "paused_by": None}
    active_restaurant = {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10",
        "features": active_features,
    }
    db.db_get_restaurant_by_id = AsyncMock(return_value=active_restaurant)

    merge_mock = AsyncMock(return_value=active_features)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/pause",
        json={"paused": False},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200, resp.text
    patch_arg = merge_mock.call_args[0][1]
    assert patch_arg["bot_active"] is True
    assert patch_arg["paused_at"] is None
    assert patch_arg["paused_by"] is None


def test_pause_stores_timestamp_and_user(monkeypatch):
    """POST /api/settings/pause with paused=true stores paused_at and paused_by."""
    _pause_auth_patches(monkeypatch)

    from app.repositories import restaurant_repo
    from app.services import database as db

    paused_features = {"bot_active": False, "paused_at": "2026-04-20T12:00:00Z", "paused_by": "owner_test"}
    paused_restaurant = {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10",
        "features": paused_features,
    }
    db.db_get_restaurant_by_id = AsyncMock(return_value=paused_restaurant)

    merge_mock = AsyncMock(return_value=paused_features)
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/pause",
        json={"paused": True},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["paused"] is True
    assert "paused_at" in data
    assert "paused_by" in data

    patch_arg = merge_mock.call_args[0][1]
    assert "paused_at" in patch_arg
    assert patch_arg["paused_by"] == "owner_test"


def test_pause_tenant_isolation(monkeypatch):
    """POST /api/settings/pause only affects the authenticated restaurant's org."""
    _pause_auth_patches(monkeypatch)

    from app.repositories import restaurant_repo
    from app.services import database as db

    paused_restaurant = {
        "id": RESTAURANT_ID,
        "org_id": RESTAURANT_ID,
        "location_id": RESTAURANT_ID,
        "name": "El Fogón",
        "whatsapp_number": "573001234567",
        "address": "Calle 10",
        "features": {"bot_active": False, "paused_at": "2026-04-20T12:00:00Z"},
    }
    db.db_get_restaurant_by_id = AsyncMock(return_value=paused_restaurant)

    merge_mock = AsyncMock(return_value=paused_restaurant["features"])
    monkeypatch.setattr(restaurant_repo, "db_merge_restaurant_features", merge_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/settings/pause",
        json={"paused": True},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 200

    # Verify db_merge_restaurant_features was called with this restaurant's ID
    call_args = merge_mock.call_args[0]
    assert call_args[0] == RESTAURANT_ID


def test_pause_requires_owner_or_admin(monkeypatch):
    """POST /api/settings/pause returns 403 for gerente role."""
    _pause_auth_patches(monkeypatch, role="gerente")

    client = TestClient(app)
    resp = client.post(
        "/api/settings/pause",
        json={"paused": True},
        headers={"Authorization": "Bearer faketoken"},
    )
    assert resp.status_code == 403
    assert "admin" in resp.json()["detail"].lower() or "owner" in resp.json()["detail"].lower()
