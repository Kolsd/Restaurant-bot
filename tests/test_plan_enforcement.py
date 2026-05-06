"""
tests/test_plan_enforcement.py

Unit tests for app/services/plan_enforcement.py — subscription cap enforcement.

These are pure unit tests (no TEST_DATABASE_URL required). All repo functions
and Redis helpers are mocked. Tests validate the decision tree exactly as
specified in the sprint task.

Test matrix
-----------
  test_under_cap_proceed              — status='ok' → PROCEED, increment called once
  test_comp_tenant_proceed            — status='comp' → PROCEED, no pack logic
  test_exceeded_pack_credits_proceed  — exceeded + pack credits → consume 1 credit, PROCEED
  test_exceeded_auto_recharge_fires   — exceeded + auto-recharge enabled + under max → create pack, PROCEED
  test_exceeded_no_recharge_redirect  — exceeded + auto-recharge disabled → REDIRECT_TO_HUMAN
  test_exceeded_max_packs_reached_redirect — exceeded + auto-recharge but packs maxed → REDIRECT
  test_threshold_50_first_cross       — first crossing of 50% → notification sent once
  test_threshold_80_first_cross       — first crossing of 80% → notification sent once
  test_threshold_no_double_fire       — second call at same threshold → no duplicate notification
  test_db_check_caps_error_fail_open  — db_check_caps raises → PROCEED (fail-open, bot not silenced)
  test_no_caps_data_proceed           — db_check_caps returns {} → PROCEED
  test_create_pack_failure_redirect   — auto-recharge enabled but db_create_pack fails → REDIRECT
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from app.services.plan_enforcement import (
    CapDecision,
    REDIRECT_MESSAGE,
    check_and_consume_conv_slot,
    _fb_threshold_warns,
)
from app.services.tenant_context import tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_caps(status: str = "ok", used: int = 0, cap: int = 1000, pack_credits: int = 0):
    """Return a caps dict shaped like db_check_caps output."""
    return {
        "comp_active": status == "comp",
        "conv": {
            "used": used,
            "cap": cap,
            "pack_credits": pack_credits,
            "available": max(0, cap - used) + pack_credits,
            "pct": round(used / cap, 4) if cap > 0 else 0.0,
            "status": status,
        },
        "audio": {"used": 0.0, "cap": 300, "pct": 0.0, "status": "ok"},
    }


def _make_sub(auto_recharge_enabled: bool = False, max_packs: int = 0):
    return {
        "plan_code": "starter",
        "auto_recharge_enabled": auto_recharge_enabled,
        "auto_recharge_max_packs_per_month": max_packs,
    }


ORG_ID = 42

# Patch paths used in all tests
_REPO = "app.services.plan_enforcement"


# ── Test 1: Under cap → PROCEED ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_under_cap_proceed():
    """status='ok' → PROCEED, db_increment_conv_usage called exactly once."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, return_value=_make_caps("ok", 10, 100)) as mock_caps,
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=11) as mock_incr,
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock) as mock_consume,
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock) as mock_create,
        patch(f"{_REPO}._fire_threshold_warn_nowait"),  # suppress side-effect
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_caps.assert_called_once_with(ORG_ID)
    mock_incr.assert_called_once_with(ORG_ID)
    mock_consume.assert_not_called()
    mock_create.assert_not_called()


# ── Test 2: Comp tenant → PROCEED without cap math ───────────────────────────

@pytest.mark.asyncio
async def test_comp_tenant_proceed():
    """status='comp' → PROCEED; no pack consumption or auto-recharge attempted."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, return_value=_make_caps("comp")) as mock_caps,
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=1) as mock_incr,
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock) as mock_consume,
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock) as mock_create,
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock) as mock_sub,
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_incr.assert_called_once_with(ORG_ID)
    mock_consume.assert_not_called()
    mock_create.assert_not_called()
    mock_sub.assert_not_called()


# ── Test 3: Exceeded + pack credits → consume pack, PROCEED ──────────────────

@pytest.mark.asyncio
async def test_exceeded_pack_credits_proceed():
    """exceeded status + pack_credits > 0 → db_consume_pack_credit called, PROCEED."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock,
              return_value=_make_caps("exceeded", 100, 100, pack_credits=50)) as mock_caps,
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=101) as mock_incr,
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock, return_value=1) as mock_consume,
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock) as mock_create,
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock) as mock_sub,
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_consume.assert_called_once_with(ORG_ID, count=1)
    mock_incr.assert_called_once_with(ORG_ID)
    mock_create.assert_not_called()
    mock_sub.assert_not_called()


# ── Test 4: Exceeded + auto-recharge enabled + under max → fire pack, PROCEED ─

@pytest.mark.asyncio
async def test_exceeded_auto_recharge_fires():
    """exceeded + auto_recharge enabled + packs_used < max → db_create_pack called, PROCEED."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock,
              return_value=_make_caps("exceeded", 100, 100, pack_credits=0)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=101),
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock, return_value=1),
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock, return_value=99) as mock_create,
        patch(f"{_REPO}.db_count_packs_this_period", new_callable=AsyncMock, return_value=1) as mock_count,
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock,
              return_value=_make_sub(auto_recharge_enabled=True, max_packs=5)) as mock_sub,
        patch(f"{_REPO}._send_recharge_notification_nowait"),
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_create.assert_called_once_with(
        ORG_ID, credits=100, amount_paid_cop=50_000, fired_automatically=True
    )
    mock_count.assert_called_once_with(ORG_ID)


# ── Test 5: Exceeded + auto-recharge disabled → REDIRECT_TO_HUMAN ────────────

@pytest.mark.asyncio
async def test_exceeded_no_recharge_redirect():
    """exceeded + auto_recharge disabled → REDIRECT_TO_HUMAN."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock,
              return_value=_make_caps("exceeded", 100, 100, pack_credits=0)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock),
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock, return_value=0),
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock) as mock_create,
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock,
              return_value=_make_sub(auto_recharge_enabled=False, max_packs=0)),
        patch(f"{_REPO}._send_cap_exhausted_alert_nowait"),
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.REDIRECT_TO_HUMAN
    mock_create.assert_not_called()


# ── Test 6: Exceeded + auto-recharge enabled but max packs reached → REDIRECT ─

@pytest.mark.asyncio
async def test_exceeded_max_packs_reached_redirect():
    """exceeded + auto_recharge enabled but packs_used >= max_packs → REDIRECT_TO_HUMAN."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock,
              return_value=_make_caps("exceeded", 100, 100, pack_credits=0)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock),
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock, return_value=0),
        patch(f"{_REPO}.db_create_pack", new_callable=AsyncMock) as mock_create,
        patch(f"{_REPO}.db_count_packs_this_period", new_callable=AsyncMock, return_value=5),
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock,
              return_value=_make_sub(auto_recharge_enabled=True, max_packs=5)),
        patch(f"{_REPO}._send_cap_exhausted_alert_nowait"),
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.REDIRECT_TO_HUMAN
    mock_create.assert_not_called()


# ── Test 7: db_check_caps error → fail-open (PROCEED) ────────────────────────

@pytest.mark.asyncio
async def test_db_check_caps_error_fail_open():
    """If db_check_caps raises, bot must NOT be silenced — PROCEED (fail-open)."""
    with (
        patch(f"{_REPO}.db_check_caps", side_effect=RuntimeError("DB timeout")),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock) as mock_incr,
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_incr.assert_not_called()  # we fail before increment in the caps-error path


# ── Test 8: No caps data → PROCEED (pre-launch / no plan configured) ──────────

@pytest.mark.asyncio
async def test_no_caps_data_proceed():
    """db_check_caps returns {} → PROCEED without touching other repos."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, return_value={}),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock) as mock_incr,
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.PROCEED
    mock_incr.assert_not_called()


# ── Test 9: auto-recharge create_pack fails → REDIRECT ───────────────────────

@pytest.mark.asyncio
async def test_create_pack_failure_redirect():
    """auto-recharge enabled, but db_create_pack raises → REDIRECT_TO_HUMAN (no cap room)."""
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock,
              return_value=_make_caps("exceeded", 100, 100, pack_credits=0)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock),
        patch(f"{_REPO}.db_consume_pack_credit", new_callable=AsyncMock, return_value=0),
        patch(f"{_REPO}.db_create_pack", side_effect=RuntimeError("insert failed")),
        patch(f"{_REPO}.db_count_packs_this_period", new_callable=AsyncMock, return_value=0),
        patch(f"{_REPO}.db_get_org_subscription", new_callable=AsyncMock,
              return_value=_make_sub(auto_recharge_enabled=True, max_packs=5)),
        patch(f"{_REPO}._send_cap_exhausted_alert_nowait"),
    ):
        with tenant_scope(ORG_ID):
            result = await check_and_consume_conv_slot(ORG_ID)

    assert result == CapDecision.REDIRECT_TO_HUMAN


# ── Test 10: Threshold 50% first crossing → dedup slot acquired ───────────────

@pytest.mark.asyncio
async def test_threshold_50_first_cross():
    """First crossing of 50% cap → _acquire_threshold_warn_slot returns True (first time)."""
    from app.services.plan_enforcement import _fb_acquire_threshold_warn
    _fb_threshold_warns.clear()

    # Simulate: org_id=99, cap=100, prev_used=49, new_used=50
    acquired_first = _fb_acquire_threshold_warn(99, 50)
    acquired_second = _fb_acquire_threshold_warn(99, 50)

    assert acquired_first is True
    assert acquired_second is False  # dedup: second call for same (org, pct) blocked


# ── Test 11: Threshold dedup — second call does not re-fire ──────────────────

@pytest.mark.asyncio
async def test_threshold_no_double_fire():
    """_acquire_threshold_warn_slot for same (org, pct) returns False on repeat."""
    from app.services.plan_enforcement import _fb_acquire_threshold_warn
    _fb_threshold_warns.clear()

    org = 777

    first = _fb_acquire_threshold_warn(org, 80)
    second = _fb_acquire_threshold_warn(org, 80)
    different_pct = _fb_acquire_threshold_warn(org, 90)

    assert first is True
    assert second is False   # same threshold blocked
    assert different_pct is True  # different threshold is independent


# ── Test 12: _fire_threshold_warn selects correct threshold bucket ────────────

@pytest.mark.asyncio
async def test_threshold_80_first_cross():
    """_fire_threshold_warn sends the 80-pct message when prev<80<=new."""
    _fb_threshold_warns.clear()

    sent_messages = []

    async def _mock_send(bot_number, access_token, phone, text, phone_id):
        sent_messages.append(text)

    caps = _make_caps("ok", 79, 100)

    with (
        patch(f"{_REPO}._acquire_threshold_warn_slot", new_callable=AsyncMock, return_value=True),
        patch(f"{_REPO}._send_wa_best_effort", side_effect=_mock_send),
    ):
        from app.services.plan_enforcement import _fire_threshold_warn
        await _fire_threshold_warn(
            org_id=ORG_ID,
            new_used=80,
            caps=caps,
            bot_number="bot",
            access_token="tok",
            admin_phone="admin",
            phone_id="pid",
        )

    assert len(sent_messages) == 1
    assert "80%" in sent_messages[0]


# ── Test 13: REDIRECT_MESSAGE is the correct customer copy ───────────────────

def test_redirect_message_content():
    """REDIRECT_MESSAGE must contain the specified human-redirect copy."""
    assert "asesor humano" in REDIRECT_MESSAGE
    assert "momentico" in REDIRECT_MESSAGE
