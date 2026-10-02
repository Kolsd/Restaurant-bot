"""
tests/test_internal_admin_plan_reset.py

Unit tests for the 3 new superadmin endpoint handlers:
  1. change_org_plan   — PATCH /organizations/{id}/plan (validation + 404 only)
  2. set_org_comp      — PATCH /organizations/{id}/comp (404 only)
  (The set-password endpoint was removed 2026-10-02: Mesio never sets a
   password — tests/test_hq_support.py covers the email-code reset.)

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
