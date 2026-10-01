"""
tests/test_plan_enforcement.py

Unit tests for app/services/plan_enforcement.py — conversation metering.

PM decision 2026-09-23 (flat price per sede): the plan's conversation
allowance is an internal soft ceiling. These tests pin that it can never
silence the bot or charge the restaurant: past the cap the conversation is
still counted and Mesio is alerted, and nothing creates or consumes packs.
No TEST_DATABASE_URL required — repo functions and Redis helpers are mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import app.services.plan_enforcement as pe
from app.services.plan_enforcement import _fb_threshold_warns, record_conversation


def _make_caps(status: str = "ok", used: int = 0, cap: int = 1000):
    """Return a caps dict shaped like db_check_caps output."""
    return {
        "comp_active": status == "comp",
        "conv": {"used": used, "cap": cap, "pack_credits": 0, "status": status},
    }


ORG_ID = 42
_REPO = "app.services.plan_enforcement"


def test_nothing_can_cut_the_bot_off_or_charge():
    """The redirect-to-human reply and the pack auto-charge are gone."""
    for name in ("CapDecision", "REDIRECT_MESSAGE", "check_and_consume_conv_slot",
                 "db_create_pack", "db_consume_pack_credit"):
        assert not hasattr(pe, name), name


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ok", "warn90", "exceeded"])
async def test_every_message_is_counted_whatever_the_cap(status):
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, return_value=_make_caps(status, 100, 100)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=101) as incr,
        patch(f"{_REPO}._fire_threshold_warn_nowait") as warn,
    ):
        assert await record_conversation(ORG_ID) is None
    incr.assert_awaited_once_with(ORG_ID)
    warn.assert_called_once_with(ORG_ID, 101, _make_caps(status, 100, 100))


@pytest.mark.asyncio
async def test_comp_tenant_is_counted_without_warnings():
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, return_value=_make_caps("comp", 5, 100)),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock, return_value=6) as incr,
        patch(f"{_REPO}._fire_threshold_warn_nowait") as warn,
    ):
        await record_conversation(ORG_ID)
    incr.assert_awaited_once_with(ORG_ID)
    warn.assert_not_called()


@pytest.mark.asyncio
async def test_metering_errors_never_reach_the_bot():
    with (
        patch(f"{_REPO}.db_check_caps", new_callable=AsyncMock, side_effect=RuntimeError("db down")),
        patch(f"{_REPO}.db_increment_conv_usage", new_callable=AsyncMock) as incr,
    ):
        assert await record_conversation(ORG_ID) is None
    incr.assert_not_awaited()


@pytest.mark.asyncio
async def test_crossing_the_cap_alerts_once():
    """Reaching 100% is logged for Mesio exactly once per period."""
    with (
        patch(f"{_REPO}._acquire_threshold_warn_slot", new_callable=AsyncMock, return_value=True) as slot,
        patch(f"{_REPO}.log") as log_mock,
    ):
        await pe._fire_threshold_warn(org_id=ORG_ID, new_used=100, caps=_make_caps("ok", 99, 100))
    slot.assert_awaited_once_with(ORG_ID, 100)
    crossed = [c for c in log_mock.warning.call_args_list
               if c.args and c.args[0] == "plan_enforcement.usage_threshold_crossed"]
    assert [c.kwargs["pct"] for c in crossed] == [100]


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
    """_fire_threshold_warn logs the 80-pct crossing once when prev<80<=new.

    (It sent the owner a WhatsApp until 2026-09-25; Mesio now sees usage in
    /internal notifications, and the crossing is logged here.)"""
    _fb_threshold_warns.clear()

    caps = _make_caps("ok", 79, 100)

    with (
        patch(f"{_REPO}._acquire_threshold_warn_slot", new_callable=AsyncMock, return_value=True) as slot,
        patch(f"{_REPO}.log") as log_mock,
    ):
        from app.services.plan_enforcement import _fire_threshold_warn
        await _fire_threshold_warn(org_id=ORG_ID, new_used=80, caps=caps)

    slot.assert_awaited_once_with(ORG_ID, 80)
    crossed = [c for c in log_mock.warning.call_args_list
               if c.args and c.args[0] == "plan_enforcement.usage_threshold_crossed"]
    assert len(crossed) == 1
    assert crossed[0].kwargs["pct"] == 80
    assert crossed[0].kwargs["used"] == 80
