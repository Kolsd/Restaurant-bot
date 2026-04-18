"""
Suite 1 — Core: Authentication, Rate Limiting, Multi-tenancy
tests/test_core.py

Covers:
  1. Login success — bcrypt verification → token issued
  2. Login wrong password → failure dict
  3. Login endpoint rate limit → 429 after _LOGIN_MAX attempts
  4. require_module: flag absent (opt-out model) → 403 (feature not enabled)
  5. require_module: flag explicitly True → 200
  6. require_module: flag explicitly False → 403
  7. db_check_module unit: True in JSONB → True
  8. db_check_module unit: False in JSONB → False
  9. db_check_module unit: key absent → False
 10. db_check_module unit: restaurant not found → False
 11. GET protected endpoint without token → 401
"""
import time
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import make_pool, make_row, patch_auth


# ══════════════════════════════════════════════════════════════════════════════
# 1–2. auth.login (pure service layer, no HTTP)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_login_success():
    """Valid credentials produce a success dict with a token."""
    from app.services import auth
    from app.services.auth import hash_password

    hashed = hash_password("supersecreta")

    mock_user = {
        "username":        "owner",
        "restaurant_name": "El Bistro",
        "branch_id":       1,
        "role":            "owner",
        "password_hash":   hashed,
    }
    mock_restaurant = {"id": 1, "whatsapp_number": "+57300", "name": "El Bistro", "features": {}}
    fake_token = "a" * 64

    mock_location = {
        "id": 1, "org_id": 1, "name": "El Bistro",
        "is_primary": True, "whatsapp_number": "+57300", "active": True,
    }
    mock_org = {
        "id": 1, "name": "El Bistro", "whatsapp_number": "+57300",
        "features": {}, "subscription_plan": "free",
    }
    mock_org_locations = [mock_location]

    with (
        patch.object(auth.db, "db_get_user",           AsyncMock(return_value=mock_user)),
        patch.object(auth.db, "db_get_restaurant_by_id", AsyncMock(return_value=mock_restaurant)),
        patch("app.repositories.sessions_repo.create_session", AsyncMock(return_value=fake_token)),
        patch("app.repositories.restaurant_repo.db_get_location_by_id", AsyncMock(return_value=mock_location)),
        patch("app.repositories.restaurant_repo.db_get_org_by_id",      AsyncMock(return_value=mock_org)),
        patch("app.repositories.restaurant_repo.db_get_org_locations",  AsyncMock(return_value=mock_org_locations)),
    ):
        result = await auth.login("owner", "supersecreta")

    assert result["success"] is True
    assert result["token"] == fake_token
    assert result["restaurant"]["role"] == "owner"


@pytest.mark.asyncio
async def test_login_wrong_password():
    """Wrong password returns success=False, no token."""
    from app.services import auth
    from app.services.auth import hash_password

    mock_user = {
        "username":      "owner",
        "restaurant_name": "El Bistro",
        "branch_id":     1,
        "role":          "owner",
        "password_hash": hash_password("correct_password"),
    }

    with patch.object(auth.db, "db_get_user", AsyncMock(return_value=mock_user)):
        result = await auth.login("owner", "wrong_password")

    assert result["success"] is False
    assert "token" not in result


@pytest.mark.asyncio
async def test_login_user_not_found():
    """Non-existent username returns success=False."""
    from app.services import auth

    with (
        patch.object(auth.db, "db_get_user", AsyncMock(return_value=None)),
        patch.object(auth.db, "db_get_staff_candidates_by_name", AsyncMock(return_value=[])),
    ):
        result = await auth.login("nadie", "pass")

    assert result["success"] is False


@pytest.mark.asyncio
async def test_login_legacy_sha256_triggers_bcrypt_upgrade():
    """Admin login with legacy sha256 hash succeeds AND rehashes to bcrypt."""
    import hashlib
    from app.services import auth

    legacy_hash = hashlib.sha256("supersecreta".encode()).hexdigest()
    mock_user = {
        "username":        "owner",
        "restaurant_name": "El Bistro",
        "branch_id":       1,
        "role":            "owner",
        "password_hash":   legacy_hash,
    }
    mock_restaurant = {"id": 1, "whatsapp_number": "+57300", "name": "El Bistro", "features": {}}
    mock_location = {"id": 1, "org_id": 1, "name": "El Bistro", "is_primary": True, "whatsapp_number": "+57300", "active": True}
    mock_org = {"id": 1, "name": "El Bistro", "whatsapp_number": "+57300", "features": {}, "subscription_plan": "free"}
    update_mock = AsyncMock(return_value=True)

    with (
        patch.object(auth.db, "db_get_user", AsyncMock(return_value=mock_user)),
        patch.object(auth.db, "db_get_restaurant_by_id", AsyncMock(return_value=mock_restaurant)),
        patch.object(auth.db, "db_update_user_password", update_mock),
        patch("app.repositories.sessions_repo.create_session", AsyncMock(return_value="t" * 64)),
        patch("app.repositories.restaurant_repo.db_get_location_by_id", AsyncMock(return_value=mock_location)),
        patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=mock_org)),
        patch("app.repositories.restaurant_repo.db_get_org_locations", AsyncMock(return_value=[mock_location])),
    ):
        result = await auth.login("owner", "supersecreta")

    assert result["success"] is True
    update_mock.assert_awaited_once()
    new_hash = update_mock.await_args.args[1]
    assert new_hash.startswith("$2"), f"Expected bcrypt hash, got: {new_hash[:10]}"


@pytest.mark.asyncio
async def test_login_bcrypt_user_no_upgrade():
    """Login with already-bcrypt hash must NOT call db_update_user_password."""
    from app.services import auth
    from app.services.auth import hash_password

    mock_user = {
        "username":        "owner",
        "restaurant_name": "El Bistro",
        "branch_id":       1,
        "role":            "owner",
        "password_hash":   hash_password("supersecreta"),
    }
    mock_restaurant = {"id": 1, "whatsapp_number": "+57300", "name": "El Bistro", "features": {}}
    mock_location = {"id": 1, "org_id": 1, "name": "El Bistro", "is_primary": True, "whatsapp_number": "+57300", "active": True}
    mock_org = {"id": 1, "name": "El Bistro", "whatsapp_number": "+57300", "features": {}, "subscription_plan": "free"}
    update_mock = AsyncMock(return_value=True)

    with (
        patch.object(auth.db, "db_get_user", AsyncMock(return_value=mock_user)),
        patch.object(auth.db, "db_get_restaurant_by_id", AsyncMock(return_value=mock_restaurant)),
        patch.object(auth.db, "db_update_user_password", update_mock),
        patch("app.repositories.sessions_repo.create_session", AsyncMock(return_value="t" * 64)),
        patch("app.repositories.restaurant_repo.db_get_location_by_id", AsyncMock(return_value=mock_location)),
        patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=mock_org)),
        patch("app.repositories.restaurant_repo.db_get_org_locations", AsyncMock(return_value=[mock_location])),
    ):
        result = await auth.login("owner", "supersecreta")

    assert result["success"] is True
    update_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# 3. Login endpoint rate limit
# ══════════════════════════════════════════════════════════════════════════════

def test_login_rate_limit(client, monkeypatch):
    """
    After _LOGIN_MAX failed attempts from the same IP, subsequent calls
    to POST /api/auth/login must return 429.

    Rate limiting now uses state_store.rate_limit_check (Redis / in-process fallback).
    We mock rate_limit_check to count calls and return False on the (MAX+1)th attempt.
    """
    import app.routes.auth_routes as auth_mod
    from app.services import state_store

    max_attempts = auth_mod._LOGIN_MAX  # 10

    # Counter to simulate exhausting the rate limit
    call_count = {"n": 0}

    async def mock_rate_limit_check(key, max_requests, window_seconds):
        call_count["n"] += 1
        return call_count["n"] <= max_requests  # True (allowed) until limit reached

    monkeypatch.setattr(state_store, "rate_limit_check", mock_rate_limit_check)

    # Mock auth.login to always fail quickly (no DB needed).
    async def mock_login(username, password):
        return {"success": False, "error": "bad credentials"}

    monkeypatch.setattr(auth_mod, "login", mock_login)

    # Exhaust all allowed attempts
    for i in range(max_attempts):
        r = client.post(
            "/api/auth/login",
            json={"username": "test", "password": "bad"},
            headers={"X-Forwarded-For": "10.0.0.99"},
        )
        # Each attempt before the limit returns 401 (failed auth, but not rate-limited)
        assert r.status_code in (200, 401), f"Unexpected status on attempt {i+1}: {r.status_code}"

    # The (max_attempts+1)-th attempt must be rate-limited
    r = client.post(
        "/api/auth/login",
        json={"username": "test", "password": "bad"},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )
    assert r.status_code == 429, f"Expected 429 on attempt {max_attempts+1}, got {r.status_code}"
    assert "Too many" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# 4–6. require_module dependency — HTTP-level 403 enforcement
# ══════════════════════════════════════════════════════════════════════════════

def test_require_module_absent_flag_returns_403(client, monkeypatch):
    """
    When features does not contain the module key, db_check_module returns False
    and the endpoint must return 403.
    """
    patch_auth(monkeypatch, features={})  # staff_tips absent → False
    monkeypatch.setattr("app.services.database.db_check_module",
                        AsyncMock(return_value=False))

    r = client.get("/api/staff", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403
    assert "staff_tips" in r.json()["detail"]


def test_require_module_flag_true_allows_access(client, monkeypatch):
    """
    When features.staff_tips = true, db_check_module returns True and the
    endpoint proceeds (200, not 403).
    """
    patch_auth(monkeypatch, features={"staff_tips": True})
    monkeypatch.setattr("app.services.database.db_check_module",
                        AsyncMock(return_value=True))

    # Also mock the DB call inside the endpoint itself
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_get_staff", AsyncMock(return_value=[]))

    r = client.get("/api/staff", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200


def test_require_module_flag_false_returns_403(client, monkeypatch):
    """
    When features.staff_tips is explicitly False, the endpoint must return 403.
    """
    patch_auth(monkeypatch, features={"staff_tips": False})
    monkeypatch.setattr("app.services.database.db_check_module",
                        AsyncMock(return_value=False))

    r = client.get("/api/staff", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# 7–10. db_check_module unit tests (DB layer)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_check_module_true():
    """fetchval returns True (JSONB flag = 'true') → db_check_module returns True."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=True)

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        result = await db.db_check_module("+573001234567", "staff_tips")

    assert result is True


@pytest.mark.asyncio
async def test_db_check_module_false():
    """fetchval returns False (JSONB flag = 'false') → db_check_module returns False."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=False)

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        result = await db.db_check_module("+573001234567", "staff_tips")

    assert result is False


@pytest.mark.asyncio
async def test_db_check_module_key_absent():
    """fetchval returns None (key absent in JSONB) → db_check_module returns False."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        result = await db.db_check_module("+573001234567", "nonexistent_module")

    assert result is False


@pytest.mark.asyncio
async def test_db_check_module_restaurant_not_found():
    """No matching restaurant row → fetchval returns None → False."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=None)

    with patch.object(db, "get_pool", AsyncMock(return_value=make_pool(mock_conn))):
        result = await db.db_check_module("+5599999999", "staff_tips")

    assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# 11. Unauthenticated request → 401
# ══════════════════════════════════════════════════════════════════════════════

def test_unauthenticated_request_returns_401(client, monkeypatch):
    """Any protected endpoint without a valid token must return 401."""
    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value=None))

    r = client.get("/api/staff", headers={"Authorization": "Bearer invalid"})
    assert r.status_code == 401


def test_missing_auth_header_returns_401(client, monkeypatch):
    """Request with no Authorization header must return 401."""
    monkeypatch.setattr("app.routes.deps.verify_token",
                        AsyncMock(return_value=None))

    r = client.get("/api/staff")
    assert r.status_code == 401
