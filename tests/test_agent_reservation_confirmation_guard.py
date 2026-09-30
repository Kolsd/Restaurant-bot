"""
tests/test_agent_reservation_confirmation_guard.py

Regression for agent.py guard 3c: a confirmation-word gate for
make_reservation, mirroring guard #3 (place_order / create_delivery_order /
create_pickup_order).

Real-LLM run 2026-09-13 (python run_ai_sim.py, run-20260913_040614,
mesa_05_reserva_fecha_relativa): the customer said "quiero reservar para
mañana a las 7pm, somos 4, soy Carlos"; the bot asked for the exact date;
customer said "para el 20 de diciembre"; the bot's reply THAT SAME TURN
sounded confirmatory ("Confirmo tu reserva para el 20 de diciembre...") and
make_reservation actually fired — with a provisional/wrong date (DB showed
2025-01-08) — before the customer had said anything like "sí"/"confirmo".
The customer then said "sí confirmo" and make_reservation fired AGAIN, this
time with the correct date (2024-12-20). Because the two tool_input payloads
had different dates, `_make_reservation_fingerprint` produced two different
keys, so the existing dedup guard (3d, tests/test_make_reservation_dedup.py)
did NOT catch them as duplicates — TWO reservations were created for one
customer intent.

Fix: guard 3c in app/services/agent.py requires an explicit confirmation
word (via the same `_last_messages_have_confirmation` used by orders) in the
last 2 user turns before make_reservation is allowed to actually execute,
same as orders already required. Only enforced once name/date/time are all
present, so it never masks guard #6's "falta el nombre/fecha/hora" prompt.
"""
import pytest
from unittest.mock import AsyncMock

from app.services.agent import _validate_tool_call


_FULL_RESERVATION_INPUT = {
    "name": "Carlos",
    "date": "2026-12-20",
    "time": "19:00",
    "guests": 4,
}


@pytest.mark.asyncio
async def test_make_reservation_blocked_without_prior_confirmation(monkeypatch):
    """The exact bug scenario: make_reservation called with complete, valid
    data but NO confirmation word anywhere in recent history — must be
    downgraded to no tool call, with a confirmation QUESTION (not the LLM's
    own "ya reservado"-style text) sent back instead."""
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input=dict(_FULL_RESERVATION_INPUT),
        reply="Confirmo tu reserva para el 20 de diciembre a las 19:00 para 4 personas.",
        table_context=None,
        org_id=4242,
        phone="+573001234567",
        full_history=[
            {"role": "user", "content": "quiero reservar para mañana a las 7pm, somos 4, soy Carlos"},
            {"role": "assistant", "content": "¿Cuál es la fecha exacta de mañana?"},
        ],
        user_message="para el 20 de diciembre",
    )

    assert tool_name is None, "Reservation must NOT execute without an explicit confirmation word"
    assert reply is not None
    assert "2026-12-20" in reply  # references the actual reservation data (as given in tool_input)
    assert "confirma" in reply.lower()
    # Must never be the LLM's own optimistic "confirmo tu reserva" text —
    # that would tell the customer the reservation already happened.
    assert reply != "Confirmo tu reserva para el 20 de diciembre a las 19:00 para 4 personas."
    rl_mock.assert_not_awaited()  # blocked before ever reaching the dedup/rate-limit guard


@pytest.mark.asyncio
async def test_make_reservation_allowed_after_confirmation_word(monkeypatch):
    """Once the customer actually says a confirmation word ("sí confirmo"),
    the SAME reservation data must be allowed through."""
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input=dict(_FULL_RESERVATION_INPUT),
        reply="¡Listo! Tu reserva quedó registrada.",
        table_context=None,
        org_id=4242,
        phone="+573001234567",
        full_history=[
            {"role": "user", "content": "para el 20 de diciembre"},
        ],
        user_message="sí confirmo",
    )

    assert tool_name == "make_reservation"
    rl_mock.assert_awaited()


@pytest.mark.asyncio
async def test_two_calls_without_confirmation_never_both_create_a_reservation(monkeypatch):
    """Reproduces the exact double-booking bug end to end at the guard level:
    a premature call with a wrong date, followed by a real-confirmation call
    with the corrected date. Before the fix, BOTH would reach
    `make_reservation` (different fingerprints, dedup guard doesn't help).
    After the fix, the premature (unconfirmed) call is blocked."""
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    # Turn A: bot fires make_reservation with a provisional/wrong date, with
    # NO confirmation word from the customer yet.
    tool_name_a, reply_a, _ = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input={**_FULL_RESERVATION_INPUT, "date": "2026-01-08"},  # wrong/guessed date
        reply="Confirmo tu reserva para el 8 de enero.",
        table_context=None,
        org_id=4242,
        phone="+573001234567",
        full_history=[{"role": "user", "content": "quiero reservar para mañana, somos 4, soy Carlos"}],
        user_message="para el 20 de diciembre",
    )
    assert tool_name_a is None, "Premature call (no confirmation yet) must be blocked"

    # Turn B: customer explicitly confirms; bot fires make_reservation again
    # with the corrected date.
    tool_name_b, reply_b, _ = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input={**_FULL_RESERVATION_INPUT, "date": "2026-12-20"},  # corrected date
        reply="¡Listo! Tu reserva quedó registrada para el 20 de diciembre.",
        table_context=None,
        org_id=4242,
        phone="+573001234567",
        full_history=[{"role": "user", "content": "para el 20 de diciembre"}],
        user_message="sí confirmo",
    )
    assert tool_name_b == "make_reservation", "Real confirmation must be allowed through"

    # Only ONE of the two turns actually reached a state where the tool would
    # execute — never both, which is what created the duplicate in the bug.
    assert [tool_name_a, tool_name_b].count("make_reservation") == 1


@pytest.mark.asyncio
async def test_missing_fields_falls_through_to_field_guard_not_confirmation_guard(monkeypatch):
    """If name/date/time aren't all present yet, guard 3c must stay out of
    the way (no nonsensical "¿confirmas la reserva para  a las ?") and let
    guard #6 ask for the missing field instead."""
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="make_reservation",
        tool_input={"name": "Carlos", "date": "", "time": "19:00", "guests": 4},
        reply="",
        table_context=None,
        org_id=4242,
        phone="+573001234567",
        full_history=[],
    )

    assert tool_name is None
    assert reply is not None
    assert "fecha" in reply.lower()  # guard #6's missing-field message, not a confirmation question
    assert "confirma" not in reply.lower()


@pytest.mark.asyncio
async def test_reservation_reorder_of_dish_tools_unaffected(monkeypatch):
    """Sanity: guard 3c only looks at make_reservation — other tools (e.g.
    place_order) must be completely unaffected by its presence."""
    rl_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.agent.state_store.rate_limit_check", rl_mock)

    tool_name, reply, tool_input = await _validate_tool_call(
        tool_name="call_waiter",
        tool_input={"notes": "servilletas"},
        reply="Aviso al mesero ahora mismo.",
        table_context={"id": "t1", "name": "Mesa 1"},
        org_id=4242,
        phone="+573001234567",
        full_history=[],
    )
    assert tool_name == "call_waiter"
