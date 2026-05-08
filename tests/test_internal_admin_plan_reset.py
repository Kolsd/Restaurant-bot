"""
tests/test_internal_admin_plan_reset.py

Unit tests for the 3 new superadmin endpoint handlers:
  1. change_org_plan   — PATCH /organizations/{id}/plan
  2. set_org_comp      — PATCH /organizations/{id}/comp
  3. reset_user_password — POST /users/{username}/reset-password

Tests call the handler coroutines directly (bypassing HTTP layer) to avoid
the DB-URL-required pool initialisation that TestClient triggers.
All DB/repo calls are mocked.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from fastapi import Request


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_conn():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch    = AsyncMock(return_value=[])
    conn.execute  = AsyncMock(return_value="UPDATE 1")
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__  = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _make_request(headers=None):
    """Minimal mock of fastapi.Request."""
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value="superadmin")
    return req


_FAKE_ORG = {
    "id": 42,
    "name": "Test Org",
    "slug": "test-org",
    "whatsapp_number": "+573001234567",
    "wa_phone_id": None,
    "wa_access_token": None,
    "menu": [],
    "features": {},
    "subscription_plan": "free",
    "subscription_status": "active",
    "plan_code": "free",
    "comp_until": None,
    "created_at": None,
    "updated_at": None,
}

_FAKE_USER = {
    "username": "owner@testcafe",
    "password_hash": "$2b$12$fakehash",
    "restaurant_name": "Test Cafe",
    "role": "owner",
    "branch_id": 42,
    "parent_user": None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1 — Pydantic validation on ChangePlanRequest
# ═══════════════════════════════════════════════════════════════════════════════

def test_change_plan_request_valid_plans():
    """All Pricing v1 plan codes pass validation."""
    from app.routes.internal.admin import ChangePlanRequest

    for plan in ("pulso", "restaurante", "pro", "cadena", "comp", "free"):
        req = ChangePlanRequest(plan_code=plan)
        assert req.plan_code == plan


def test_change_plan_request_invalid_raises():
    """Stale plan codes (basic, premium, legacy) are rejected."""
    from app.routes.internal.admin import ChangePlanRequest
    from pydantic import ValidationError

    for bad in ("basic", "premium", "legacy_pro", "starter"):
        with pytest.raises(ValidationError):
            ChangePlanRequest(plan_code=bad)


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1 — change_org_plan handler
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_change_plan_success():
    """Handler returns _ok payload with old/new plan."""
    from app.routes.internal.admin import change_org_plan, ChangePlanRequest

    conn = _make_conn()
    pool = _make_pool(conn)
    req  = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=_FAKE_ORG)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", AsyncMock(return_value=1)):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await change_org_plan(
            org_id=42,
            body=ChangePlanRequest(plan_code="restaurante"),
            request=req,
        )

    assert result["success"] is True
    assert result["data"]["new_plan"] == "restaurante"
    assert result["data"]["old_plan"] == "free"
    assert result["data"]["org_id"] == 42
    # comp_until should be None when switching to a paying plan
    assert result["data"]["comp_until"] is None


@pytest.mark.asyncio
async def test_change_plan_comp_sets_comp_until_default():
    """Switching to 'comp' without explicit comp_until defaults to today+30d."""
    from app.routes.internal.admin import change_org_plan, ChangePlanRequest
    from datetime import datetime, timezone

    conn = _make_conn()
    pool = _make_pool(conn)
    req  = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=_FAKE_ORG)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", AsyncMock(return_value=1)):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await change_org_plan(
            org_id=42,
            body=ChangePlanRequest(plan_code="comp"),
            request=req,
        )

    assert result["data"]["new_plan"] == "comp"
    comp_until = result["data"]["comp_until"]
    assert comp_until is not None
    # Should be a future date (roughly today + 30 days)
    from datetime import date, timedelta
    parsed = date.fromisoformat(comp_until)
    today = date.today()
    assert parsed > today
    assert parsed <= today + timedelta(days=32)


@pytest.mark.asyncio
async def test_change_plan_comp_explicit_comp_until():
    """Switching to 'comp' with explicit comp_until uses that date."""
    from app.routes.internal.admin import change_org_plan, ChangePlanRequest

    conn = _make_conn()
    pool = _make_pool(conn)
    req  = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=_FAKE_ORG)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", AsyncMock(return_value=1)):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await change_org_plan(
            org_id=42,
            body=ChangePlanRequest(plan_code="comp", comp_until="2026-12-31"),
            request=req,
        )

    assert result["data"]["comp_until"] == "2026-12-31"


@pytest.mark.asyncio
async def test_change_plan_org_not_found_raises_404():
    """Handler raises HTTP 404 when org doesn't exist."""
    from app.routes.internal.admin import change_org_plan, ChangePlanRequest
    from fastapi import HTTPException

    req = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await change_org_plan(
                org_id=9999,
                body=ChangePlanRequest(plan_code="pulso"),
                request=req,
            )
    assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2 — set_org_comp handler
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_set_comp_with_date_sets_plan_to_comp():
    """Passing a date in comp_until sets plan_code = 'comp'."""
    from app.routes.internal.admin import set_org_comp, SetCompRequest

    conn = _make_conn()
    pool = _make_pool(conn)
    req  = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=_FAKE_ORG)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", AsyncMock(return_value=1)):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await set_org_comp(
            org_id=42,
            body=SetCompRequest(comp_until="2027-06-30"),
            request=req,
        )

    assert result["data"]["new_plan"] == "comp"
    assert result["data"]["comp_until"] == "2027-06-30"


@pytest.mark.asyncio
async def test_clear_comp_reverts_to_free():
    """Passing comp_until=None clears comp and reverts plan to 'free'."""
    from app.routes.internal.admin import set_org_comp, SetCompRequest

    conn = _make_conn()
    pool = _make_pool(conn)
    req  = _make_request()

    comp_org = dict(_FAKE_ORG, plan_code="comp", comp_until="2026-12-31")

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=comp_org)), \
         patch("app.services.database.get_pool", AsyncMock(return_value=pool)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", AsyncMock(return_value=1)):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await set_org_comp(
            org_id=42,
            body=SetCompRequest(comp_until=None),
            request=req,
        )

    assert result["data"]["comp_until"] is None
    assert result["data"]["new_plan"] == "free"


@pytest.mark.asyncio
async def test_set_comp_org_not_found_raises_404():
    """Handler raises HTTP 404 when org doesn't exist."""
    from app.routes.internal.admin import set_org_comp, SetCompRequest
    from fastapi import HTTPException

    req = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_org_by_id", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await set_org_comp(
                org_id=9999,
                body=SetCompRequest(comp_until="2027-01-01"),
                request=req,
            )
    assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3 — reset_user_password handler
# ═══════════════════════════════════════════════════════════════════════════════

def test_reset_password_request_min_length():
    """Pydantic model rejects passwords shorter than 8 chars."""
    from app.routes.internal.admin import ResetPasswordRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResetPasswordRequest(new_password="short")

    # Exactly 8 chars should pass
    req = ResetPasswordRequest(new_password="abcd1234")
    assert req.new_password == "abcd1234"


@pytest.mark.asyncio
async def test_reset_password_success():
    """Handler hashes password, deletes sessions, returns 200 payload."""
    from app.routes.internal.admin import reset_user_password, ResetPasswordRequest

    req = _make_request()
    mock_audit = AsyncMock(return_value=1)

    with patch("app.repositories.restaurant_repo.db_get_user", AsyncMock(return_value=_FAKE_USER)), \
         patch("app.repositories.restaurant_repo.db_update_user_password", AsyncMock(return_value=True)) as mock_upd_pw, \
         patch("app.repositories.sessions_repo.delete_sessions_for_user", AsyncMock(return_value=3)) as mock_del_sess, \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", mock_audit):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        result = await reset_user_password(
            username="owner@testcafe",
            body=ResetPasswordRequest(new_password="NewSecure123"),
            request=req,
        )

    assert result["success"] is True
    assert result["data"]["username"] == "owner@testcafe"
    assert result["data"]["sessions_deleted"] == 3

    # Password was updated with a hashed value (not plaintext)
    mock_upd_pw.assert_called_once()
    call_args = mock_upd_pw.call_args
    assert call_args.args[0] == "owner@testcafe"
    new_hash = call_args.args[1]
    assert new_hash != "NewSecure123"  # must be hashed
    assert new_hash.startswith("$2")   # bcrypt prefix

    mock_del_sess.assert_called_once_with("owner@testcafe")


@pytest.mark.asyncio
async def test_reset_password_user_not_found():
    """Handler raises HTTP 404 when user doesn't exist."""
    from app.routes.internal.admin import reset_user_password, ResetPasswordRequest
    from fastapi import HTTPException

    req = _make_request()

    with patch("app.repositories.restaurant_repo.db_get_user", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await reset_user_password(
                username="no-such-user",
                body=ResetPasswordRequest(new_password="ValidPass123"),
                request=req,
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reset_password_writes_audit_log():
    """Handler writes audit log entry with action='user.password_reset'."""
    from app.routes.internal.admin import reset_user_password, ResetPasswordRequest

    req = _make_request()
    mock_audit = AsyncMock(return_value=1)

    with patch("app.repositories.restaurant_repo.db_get_user", AsyncMock(return_value=_FAKE_USER)), \
         patch("app.repositories.restaurant_repo.db_update_user_password", AsyncMock(return_value=True)), \
         patch("app.repositories.sessions_repo.delete_sessions_for_user", AsyncMock(return_value=1)), \
         patch("app.services.tenant_context.bypass_tenant_scope") as mock_bp, \
         patch("app.repositories.internal.audit_log_repo.db_log_audit_event", mock_audit):

        mock_bp.return_value.__enter__ = MagicMock(return_value=None)
        mock_bp.return_value.__exit__  = MagicMock(return_value=False)

        await reset_user_password(
            username="owner@testcafe",
            body=ResetPasswordRequest(new_password="NewSecure123"),
            request=req,
        )

    mock_audit.assert_called_once()
    kwargs = mock_audit.call_args.kwargs
    assert kwargs["action"] == "user.password_reset"
    assert kwargs["target_id"] == "owner@testcafe"
    assert kwargs["target_type"] == "user"
