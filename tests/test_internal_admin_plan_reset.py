"""
tests/test_internal_admin_plan_reset.py

Unit tests for the 3 new superadmin endpoint handlers:
  1. change_org_plan   — PATCH /organizations/{id}/plan (validation + 404 only)
  2. set_org_comp      — PATCH /organizations/{id}/comp (404 only)
  3. reset_user_password — POST /users/{username}/reset-password

Tests call the handler coroutines directly (bypassing HTTP layer) to avoid
the DB-URL-required pool initialisation that TestClient triggers.
All DB/repo calls are mocked.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request


# ── Shared helpers ────────────────────────────────────────────────────────────

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
    "menu": [],
    "features": {},
    "subscription_plan": "esencial",
    "subscription_status": "active",
    "plan_code": "esencial",
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
# (plan/comp/founder behaviour runs against the real DB in test_pricing_plans.py)
# ═══════════════════════════════════════════════════════════════════════════════

def test_change_plan_request_valid_plans():
    """The four plans of the 2026-09-30 price list pass validation."""
    from app.routes.internal.admin import ChangePlanRequest

    for plan in ("esencial", "restaurante", "pro", "cadena"):
        req = ChangePlanRequest(plan_code=plan)
        assert req.plan_code == plan


def test_change_plan_request_invalid_raises():
    """Retired or unknown codes are rejected — 'comp' and 'free' have no
    plan_limits row, so writing them would violate fk_orgs_plan_code."""
    from app.routes.internal.admin import ChangePlanRequest
    from pydantic import ValidationError

    for bad in ("pulso", "comp", "free", "basic", "premium", "starter"):
        with pytest.raises(ValidationError):
            ChangePlanRequest(plan_code=bad)


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
                body=ChangePlanRequest(plan_code="esencial"),
                request=req,
            )
    assert exc_info.value.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2 — set_org_comp handler
# ═══════════════════════════════════════════════════════════════════════════════

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
