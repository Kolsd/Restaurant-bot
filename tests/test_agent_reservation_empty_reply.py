"""
tests/test_agent_reservation_empty_reply.py

Regression for the "reserve" branch of app.services.agent.execute_action
(~line 2077 onward): a reservation that commits successfully must never
return an empty string.

Real-LLM run 2026-09-13 (python run_ai_sim.py, mesa_05_reserva_fecha_relativa,
AFTER the round-2 make_reservation confirmation guard was added — see
tests/test_agent_reservation_confirmation_guard.py): once the bot correctly
waited for "sí confirmo" before calling make_reservation, Claude answered
that confirmation turn with a tool_use call and NO text block (`reply ==
""`), same failure mode as the order-confirmation bug fixed in
agent_salon.py (tests/test_feature3_dining_polish.py::test_C5_*). The
reservation WAS created in the DB (id=167, date=2024-12-20) but the customer
was shown "Disculpa, no te entendí bien..." (the top-level empty-reply
fallback in call_llm_and_execute) — misleading them about a reservation that
DID succeed.

Fix: the "reserve" branch's auto_confirm and plain-pending success paths now
fall back to a plain confirmation message when `reply` came in empty,
mirroring the fix already applied to agent_salon.py's order-success path.
"""
import pytest
from unittest.mock import AsyncMock, patch


def _restaurant_obj(**features):
    return {"id": 1, "name": "Test Rest", "whatsapp_number": "+57999", "features": features}


def _reservation_parsed(reply: str) -> dict:
    return {
        "action": "reserve",
        "reply": reply,
        "items": [],
        "reservation": {
            "name": "Carlos",
            "date": "2026-12-20",
            "time": "19:00",
            "guests": 4,
            "notes": "",
        },
    }


@pytest.mark.asyncio
async def test_reserve_empty_reply_still_confirms_pending_reservation():
    """Plain pending path (no auto_confirm, no deposits) — the common case."""
    from app.services import agent

    with (
        patch.object(agent.db, "db_get_available_tables",
                     AsyncMock(return_value=[{"id": "table-1", "capacity": 4}])),
        patch.object(agent.db, "db_add_reservation",
                     AsyncMock(return_value={"id": 167})),
        patch.object(agent.db, "db_assign_table_to_reservation", AsyncMock()),
    ):
        result = await agent.execute_action(
            parsed=_reservation_parsed(""),  # Claude's tool-only, textless response
            phone="573001234567",
            bot_number="+573009999999",
            table_context=None,
            session_state={},
            restaurant_obj=_restaurant_obj(),  # no reservation_auto_confirm/deposits flags
        )

    assert result is not None
    assert result.strip() != "", "Reservation succeeded — reply must never be empty"
    assert "Carlos" in result
    assert "2026-12-20" in result
    assert "19:00" in result


@pytest.mark.asyncio
async def test_reserve_empty_reply_still_confirms_auto_confirmed_reservation():
    """reservation_auto_confirm=True path."""
    from app.services import agent

    with (
        patch.object(agent.db, "db_get_available_tables",
                     AsyncMock(return_value=[{"id": "table-1", "capacity": 4}])),
        patch.object(agent.db, "db_add_reservation",
                     AsyncMock(return_value={"id": 168})),
        patch.object(agent.db, "db_assign_table_to_reservation", AsyncMock()),
        patch.object(agent.db, "db_confirm_reservation", AsyncMock()) as mock_confirm,
    ):
        result = await agent.execute_action(
            parsed=_reservation_parsed(""),
            phone="573001234567",
            bot_number="+573009999999",
            table_context=None,
            session_state={},
            restaurant_obj=_restaurant_obj(reservation_auto_confirm=True),
        )

    mock_confirm.assert_awaited_once()
    assert result is not None and result.strip() != ""
    assert "Carlos" in result


@pytest.mark.asyncio
async def test_reserve_non_empty_reply_from_llm_is_preserved():
    """When Claude DOES provide text, it must be used as-is (no double message,
    no override) — this fallback only kicks in for a genuinely empty reply."""
    from app.services import agent

    with (
        patch.object(agent.db, "db_get_available_tables",
                     AsyncMock(return_value=[{"id": "table-1", "capacity": 4}])),
        patch.object(agent.db, "db_add_reservation",
                     AsyncMock(return_value={"id": 169})),
        patch.object(agent.db, "db_assign_table_to_reservation", AsyncMock()),
    ):
        result = await agent.execute_action(
            parsed=_reservation_parsed("¡Reserva lista, Carlos! Te esperamos el 20 de diciembre a las 19:00."),
            phone="573001234567",
            bot_number="+573009999999",
            table_context=None,
            session_state={},
            restaurant_obj=_restaurant_obj(),
        )

    assert result == "¡Reserva lista, Carlos! Te esperamos el 20 de diciembre a las 19:00."
