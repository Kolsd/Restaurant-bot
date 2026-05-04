"""
tests/test_join_code_rate_limit.py
==================================
Unit tests for the cross-worker rate limit applied at the entry of
`_handle_join_code_flow`. Defends against brute-force code guessing
that bypasses the per-pending attempts counter (e.g. by clearing
state between guesses).

Limit: 5 numeric attempts per phone per 60-second window.

These tests do NOT require TEST_DATABASE_URL — they mock state_store
and db_link_participant_session so the flow can be exercised in
isolation. The actual rate_limit_check helper is covered separately
in app/services/state_store.py + tests/test_state_store_rate_limit.py
(if any). Here we focus on the call-site behavior.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_5_attempts_allowed_then_6th_blocked(monkeypatch):
    """Within a single 60s window, 5 numeric guesses pass through to
    db_link_participant_session, but the 6th is blocked at the rate
    limit before reaching the link function.
    """
    from app.services import agent as agent_module
    from app.services import state_store

    phone = "573009998877"
    bot = "57999000111"
    pending = {
        "table_id": "tbl-rl-test",
        "table_name": "Mesa RL",
        "attempts": 0,
        "org_id": 1,
        "location_id": None,
    }

    # Track how many times rate_limit_check was called and what counter we simulate
    rl_calls = {"count": 0}

    async def fake_rate_limit_check(key, max_requests, window_seconds):
        rl_calls["count"] += 1
        # Allow first 5, block from 6th onward
        return rl_calls["count"] <= 5

    # Track how many times the underlying link was attempted
    link_calls = {"count": 0}

    async def fake_link(**kwargs):
        link_calls["count"] += 1
        return None  # always wrong code so we get the error path

    async def noop_set(*a, **kw):
        pass

    async def noop_delete(*a, **kw):
        pass

    monkeypatch.setattr(state_store, "rate_limit_check", fake_rate_limit_check)
    monkeypatch.setattr(state_store, "join_code_pending_set", noop_set)
    monkeypatch.setattr(state_store, "join_code_pending_delete", noop_delete)

    import app.repositories.tables_repo as tr_module
    monkeypatch.setattr(tr_module, "db_link_participant_session", fake_link)

    # First 5 attempts: rate limit allows, but the per-pending counter blocks
    # at the 3rd (since _JOIN_CODE_MAX_ATTEMPTS=3). To check the rate limit
    # specifically, reset the per-pending counter each time so it never trips.
    for i in range(5):
        pending["attempts"] = 0  # always allow per-pending counter
        result = await agent_module._handle_join_code_flow(
            phone, bot, "0000", pending
        )
        # Rate limit was checked once per attempt
        assert rl_calls["count"] == i + 1
        # And db_link_participant_session was reached
        assert link_calls["count"] == i + 1
        # Returned an "incorrecto" message (wrong code path)
        msg = result["message"].lower()
        assert "incorrecto" in msg or "intento" in msg, (
            f"Attempt {i + 1}: expected 'incorrecto/intento' message, got: {result['message']!r}"
        )

    # 6th attempt: rate limit blocks BEFORE db_link_participant_session
    pending["attempts"] = 0
    result = await agent_module._handle_join_code_flow(
        phone, bot, "0000", pending
    )
    assert rl_calls["count"] == 6, "rate_limit_check should have been called a 6th time"
    assert link_calls["count"] == 5, (
        "db_link_participant_session must NOT be called once rate limit kicks in"
    )
    msg = result["message"].lower()
    assert "intentos" in msg or "minuto" in msg or "espera" in msg, (
        f"Expected rate-limit message, got: {result['message']!r}"
    )


@pytest.mark.asyncio
async def test_window_resets_after_60s(monkeypatch):
    """Simulate 5 attempts (allowed) then a window reset (rate limit allows again)
    on attempt #6 — modeled by `fake_rate_limit_check` using a flag we toggle.
    """
    from app.services import agent as agent_module
    from app.services import state_store

    phone = "573009998800"
    bot = "57999000222"
    pending = {
        "table_id": "tbl-rl-window",
        "table_name": "Mesa Win",
        "attempts": 0,
        "org_id": 1,
        "location_id": None,
    }

    state = {"window_id": 0, "in_window": 0}

    async def fake_rate_limit_check(key, max_requests, window_seconds):
        # Each call increments in_window; when toggled to a new window, reset.
        state["in_window"] += 1
        return state["in_window"] <= max_requests

    link_calls = {"count": 0}

    async def fake_link(**kwargs):
        link_calls["count"] += 1
        return None

    async def noop(*a, **kw):
        pass

    monkeypatch.setattr(state_store, "rate_limit_check", fake_rate_limit_check)
    monkeypatch.setattr(state_store, "join_code_pending_set", noop)
    monkeypatch.setattr(state_store, "join_code_pending_delete", noop)

    import app.repositories.tables_repo as tr_module
    monkeypatch.setattr(tr_module, "db_link_participant_session", fake_link)

    # 5 successful calls in first window
    for _ in range(5):
        pending["attempts"] = 0
        await agent_module._handle_join_code_flow(phone, bot, "0000", pending)
    assert link_calls["count"] == 5

    # 6th call: blocked
    pending["attempts"] = 0
    result = await agent_module._handle_join_code_flow(phone, bot, "0000", pending)
    msg = result["message"].lower()
    assert "intentos" in msg or "minuto" in msg or "espera" in msg
    assert link_calls["count"] == 5, "still 5 — 6th was blocked"

    # Simulate 60s passed → fixture: reset window counter
    state["in_window"] = 0
    state["window_id"] += 1

    # New attempt should be allowed again
    pending["attempts"] = 0
    result = await agent_module._handle_join_code_flow(phone, bot, "0000", pending)
    assert link_calls["count"] == 6, "window reset should let one more through"
    assert "incorrecto" in result["message"].lower() or "intento" in result["message"].lower()


@pytest.mark.asyncio
async def test_different_phones_independent(monkeypatch):
    """Phone A burning its 5 attempts must not block Phone B's first attempt.
    Verifies the rate limit key is keyed by phone (no leak between phones).
    """
    from app.services import agent as agent_module
    from app.services import state_store

    bot = "57999000333"
    phone_a = "573001110001"
    phone_b = "573001110002"

    # Track per-phone counters via the key passed by the agent
    counters: dict[str, int] = {}

    async def fake_rate_limit_check(key, max_requests, window_seconds):
        counters[key] = counters.get(key, 0) + 1
        return counters[key] <= max_requests

    link_calls = {"count": 0}

    async def fake_link(**kwargs):
        link_calls["count"] += 1
        return None

    async def noop(*a, **kw):
        pass

    monkeypatch.setattr(state_store, "rate_limit_check", fake_rate_limit_check)
    monkeypatch.setattr(state_store, "join_code_pending_set", noop)
    monkeypatch.setattr(state_store, "join_code_pending_delete", noop)

    import app.repositories.tables_repo as tr_module
    monkeypatch.setattr(tr_module, "db_link_participant_session", fake_link)

    pending_a = {
        "table_id": "tbl-A", "table_name": "Mesa A", "attempts": 0,
        "org_id": 1, "location_id": None,
    }
    pending_b = {
        "table_id": "tbl-B", "table_name": "Mesa B", "attempts": 0,
        "org_id": 1, "location_id": None,
    }

    # Phone A burns 5 attempts (all allowed)
    for _ in range(5):
        pending_a["attempts"] = 0
        await agent_module._handle_join_code_flow(phone_a, bot, "0000", pending_a)

    # Phone A's 6th: blocked
    pending_a["attempts"] = 0
    result_a = await agent_module._handle_join_code_flow(phone_a, bot, "0000", pending_a)
    assert "intentos" in result_a["message"].lower() or "minuto" in result_a["message"].lower()

    # Phone B's 1st: must be ALLOWED (different rate-limit key)
    pending_b["attempts"] = 0
    result_b = await agent_module._handle_join_code_flow(phone_b, bot, "1234", pending_b)
    msg_b = result_b["message"].lower()
    # Should reach db_link_participant_session and get a wrong-code response,
    # not the rate-limit one.
    assert "incorrecto" in msg_b or "intento" in msg_b, (
        f"Phone B should not be rate-limited, got: {result_b['message']!r}"
    )

    # Verify the keys were distinct
    assert any(phone_a in k for k in counters), "phone_a key not seen"
    assert any(phone_b in k for k in counters), "phone_b key not seen"
    a_keys = [k for k in counters if phone_a in k]
    b_keys = [k for k in counters if phone_b in k]
    assert a_keys != b_keys, "phones should produce different rate-limit keys"
