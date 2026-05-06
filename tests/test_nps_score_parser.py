"""Unit tests for the NPS score parser in agent._handle_nps_flow.

Regression coverage for bug found 2026-05-05 by E2E happy-path:
  Customer sends "10" expecting 10/10 (familiar with NPS 0-10 or hotel 1-10
  scales). Old regex `[1-5]` matched per-character, so "10" → "1" → score=1
  → negative-feedback flow ("¿qué podríamos mejorar?"). Pésima UX.

The fixed parser must:
  - Accept exact 1-5 single digits.
  - Accept "1 al 5" embedded in a sentence (e.g. "le doy 4").
  - Reject multi-digit numbers outside 1-5 (e.g. 10, 100) with the
    "1 al 5" reprompt — never silently truncate.
  - Reject 0 and numbers > 5.
  - Reject empty/no-digit messages.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

import pytest

from app.services import agent as agent_module
from app.services import state_store


@pytest.fixture
def nps_state():
    """Mock state_store to simulate a customer in waiting_score state."""
    with patch.object(state_store, "nps_get", new=AsyncMock(return_value={"state": "waiting_score"})):
        with patch.object(state_store, "nps_set", new=AsyncMock(return_value=None)):
            with patch.object(state_store, "nps_transition_lock_acquire", new=AsyncMock(return_value="lock-token")):
                with patch.object(state_store, "nps_transition_lock_release", new=AsyncMock(return_value=None)):
                    with patch.object(state_store, "nps_mark_done", new=AsyncMock(return_value=None)):
                        with patch.object(agent_module.db, "db_save_nps_pending", new=AsyncMock(return_value=None)):
                            with patch.object(agent_module.db, "db_save_nps_response", new=AsyncMock(return_value=None)):
                                with patch.object(agent_module.db, "db_clear_nps_waiting", new=AsyncMock(return_value=None)):
                                    yield


async def _call(message: str) -> str:
    return await agent_module._handle_nps_flow(
        phone="+57testnps", bot_number="testbot", message=message,
        restaurant_name="Test", google_maps_url="",
    )


@pytest.mark.asyncio
async def test_score_5_accepted(nps_state):
    """Single digit 5 is accepted as max-positive score."""
    reply = await _call("5")
    assert "1 al 5" not in reply, f"Score 5 should be accepted, got reprompt: {reply!r}"


@pytest.mark.asyncio
async def test_score_3_accepted(nps_state):
    """Mid score is accepted."""
    reply = await _call("3")
    assert "1 al 5" not in reply, f"Score 3 should be accepted, got reprompt: {reply!r}"


@pytest.mark.asyncio
async def test_score_1_accepted(nps_state):
    """Lowest valid score is accepted (triggers negative flow but accepted)."""
    reply = await _call("1")
    assert "1 al 5" not in reply, f"Score 1 should be accepted, got reprompt: {reply!r}"


@pytest.mark.asyncio
async def test_score_in_sentence_accepted(nps_state):
    """`'le doy 4'` should extract 4 — natural conversational rating."""
    reply = await _call("le doy 4")
    assert "1 al 5" not in reply


@pytest.mark.asyncio
async def test_multi_digit_10_rejected(nps_state):
    """REGRESSION: '10' must be rejected with reprompt, NOT parsed as 1."""
    reply = await _call("10")
    assert "1 al 5" in reply, (
        f"Score '10' must be rejected (out of 1-5 scale), but got: {reply!r}. "
        "Bug from 2026-05-05: regex [1-5] matched per-character, '10' → '1'."
    )


@pytest.mark.asyncio
async def test_multi_digit_100_rejected(nps_state):
    """100 is also out of scale."""
    reply = await _call("100")
    assert "1 al 5" in reply


@pytest.mark.asyncio
async def test_score_0_rejected(nps_state):
    """0 is below scale."""
    reply = await _call("0")
    assert "1 al 5" in reply


@pytest.mark.asyncio
async def test_score_6_rejected(nps_state):
    """6 is above scale."""
    reply = await _call("6")
    assert "1 al 5" in reply


@pytest.mark.asyncio
async def test_no_digits_rejected(nps_state):
    """Pure text reply has no score."""
    reply = await _call("muy mala")
    assert "1 al 5" in reply


@pytest.mark.asyncio
async def test_long_message_rejected(nps_state):
    """Messages over 30 chars are not parsed for score (avoids false positives in
    long complaints)."""
    long_msg = "estuvo todo muy bien aunque la mesera tardo 5 minutos en venir"
    reply = await _call(long_msg)
    assert "1 al 5" in reply


@pytest.mark.asyncio
async def test_score_5_slash_5_accepted(nps_state):
    """Common format: '5/5' or '5 de 5'."""
    reply = await _call("5/5")
    assert "1 al 5" not in reply
