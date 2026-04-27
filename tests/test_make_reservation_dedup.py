"""
tests/test_make_reservation_dedup.py
======================================
Regression test for audit bug TOP-6:

  make_reservation was missing from the dedup guard. An LLM retry or network
  glitch firing the tool twice in <30s produced two reservation rows + two
  Wompi deposit links. If the customer paid both, double deposit charged —
  money path bug with real Wompi processing fees.

Fix: dedicated dedup window keyed on (phone, bot, date+time+guests+name)
fingerprint. Blocks the second call within 60s with a friendly message.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.agent import _validate_tool_call


_RESERVATION_INPUT = {
    "name": "Miguel",
    "date": "2026-05-10",
    "time": "20:00",
    "guests": 4,
    "phone": "+573001234567",
}


@pytest.mark.asyncio
async def test_first_make_reservation_passes(monkeypatch):
    """First call within the dedup window should NOT be blocked."""
    # rate_limit_check returns True the first time the key is hit.
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input=dict(_RESERVATION_INPUT),
        reply="(LLM reply)",
        table_context=None,
        bot_number="+57888",
        phone="+573001234567",
    )

    # First call — should not be downgraded by dedup.
    assert tool_name == "make_reservation"
    rl_mock.assert_awaited()


@pytest.mark.asyncio
async def test_duplicate_make_reservation_blocked(monkeypatch):
    """Second call within window: rate_limit_check returns False → tool nullified
    and a neutral reply is returned (NOT the LLM's 'reserva confirmada' string)."""
    rl_mock = AsyncMock(return_value=False)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input=dict(_RESERVATION_INPUT),
        reply="¡Reserva confirmada!",  # LLM's optimistic reply
        table_context=None,
        bot_number="+57888",
        phone="+573001234567",
    )

    assert tool_name is None, "Duplicate reservation must downgrade tool to None"
    assert reply is not None
    # The neutral reply must NOT contain the LLM's "confirmada" string —
    # otherwise the customer thinks both reservations succeeded.
    assert "confirmada" not in reply.lower()
    assert "siendo procesada" in reply or "ya está" in reply.lower()


@pytest.mark.asyncio
async def test_different_reservation_params_not_blocked(monkeypatch):
    """Different date/time/guests/name → different fingerprint → not blocked
    (valid case: customer rebooks for a different slot in the same minute)."""
    # Track the keys that hit rate_limit_check
    seen_keys = []

    async def fake_rl(key, max_requests, window_seconds):
        seen_keys.append(key)
        return True  # always allow — we're verifying key uniqueness

    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", fake_rl)

    # First reservation: 8 PM
    await _validate_tool_call(
        tool_name="make_reservation",
        tool_input={**_RESERVATION_INPUT, "time": "20:00"},
        reply="ok",
        table_context=None,
        bot_number="+57888",
        phone="+573001234567",
    )
    # Second reservation: 9 PM — different fingerprint
    await _validate_tool_call(
        tool_name="make_reservation",
        tool_input={**_RESERVATION_INPUT, "time": "21:00"},
        reply="ok",
        table_context=None,
        bot_number="+57888",
        phone="+573001234567",
    )

    res_keys = [k for k in seen_keys if k.startswith("reservation_dedup:")]
    assert len(res_keys) == 2
    # The two keys must differ — otherwise the second reservation would be
    # blocked even though it's a legitimate different slot.
    assert res_keys[0] != res_keys[1]
