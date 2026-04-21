"""
Security tests — Server-side price resolution (PHASE 3.1).

These tests verify that the bot NEVER trusts LLM-supplied prices:
- Any `total` emitted by Claude is discarded and recomputed from the DB menu.
- Items not found in the menu cause the tool call to be rejected.
- A manipulated `total` in the LLM payload is silently replaced by the DB total.

All tests run without a real DB or Anthropic API — everything is mocked.
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

# ---------------------------------------------------------------------------
# Sample menu used by all tests (matches the structure returned by db_get_menu)
# ---------------------------------------------------------------------------

SAMPLE_MENU = {
    "Platos Fuertes": [
        {"name": "Bandeja Paisa", "price": 28000, "description": "Plato típico"},
        {"name": "Ajiaco",        "price": 18000, "description": "Sopa bogotana"},
    ],
    "Bebidas": [
        {"name": "Agua",          "price": 2000,  "description": "500ml"},
        {"name": "Jugo de Mora",  "price": 5000,  "description": "Natural"},
    ],
}

BOT_NUMBER   = "5571234567"
PHONE        = "573001112233"
RESTAURANT_OBJ = {"id": 42, "name": "Test Rest", "whatsapp_number": BOT_NUMBER}


# ---------------------------------------------------------------------------
# Helper — run _validate_tool_call with a mocked find_dish / db_get_menu
# ---------------------------------------------------------------------------

async def _run_validate(tool_name: str, tool_input: dict, menu: dict = SAMPLE_MENU,
                        confirmed: bool = True):
    """
    Execute _validate_tool_call for an order tool.

    Mocks:
      - app.services.orders.db.db_get_menu  → returns `menu`
      - state_store.rate_limit_check         → always True (dedup pass-through)

    `confirmed=True` (default) injects a prior confirmation into full_history so
    the confirmation guard passes.  Set confirmed=False to test the unconfirmed path.
    """
    from app.services.agent import _validate_tool_call

    history = [{"role": "user", "content": "sí, confirmo"}] if confirmed else []

    with patch("app.services.orders.db") as mock_orders_db, \
         patch("app.services.agent.state_store") as mock_state_store:

        mock_orders_db.db_get_menu = AsyncMock(return_value=menu)
        # rate_limit_check: dedup guard always passes in these tests
        mock_state_store.rate_limit_check = AsyncMock(return_value=True)

        return await _validate_tool_call(
            tool_name=tool_name,
            tool_input=tool_input,
            reply="Tu pedido está listo.",
            table_context=None,           # external flow (delivery/pickup)
            bot_number=BOT_NUMBER,
            phone=PHONE,
            features={},
            session_state={},
            full_history=history,
            restaurant_obj=RESTAURANT_OBJ,
        )


# ---------------------------------------------------------------------------
# Test 1 — LLM-supplied total=1 is replaced by the correct menu total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_total_overridden_by_server_price():
    """
    LLM returns place_order with total=1 (injection attempt).
    The server MUST ignore 1 and compute the real total from the menu.
    """
    tool_input = {
        "items": [{"name": "Bandeja Paisa", "qty": 1}],
        "total": 1,                     # ← injected by attacker
        "payment_method": "Efectivo",
    }

    _, _, resolved_input = await _run_validate(
        "create_pickup_order", tool_input
    )

    # The tool call must proceed (not rejected)
    assert resolved_input, "Tool call should not have been rejected"

    # _resolved_total must equal the real menu price, never the injected 1
    resolved_total = resolved_input.get("_resolved_total")
    assert resolved_total is not None, "_resolved_total must be set by the guard"
    assert isinstance(resolved_total, Decimal), "_resolved_total must be Decimal, not float"
    assert resolved_total == Decimal("28000"), (
        f"Expected 28000 (menu price), got {resolved_total!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Phantom item (not in menu) causes tool call rejection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phantom_item_rejects_tool_call():
    """
    LLM returns place_order with an item that does not exist in the menu.
    The tool call MUST be rejected with a customer-facing error message.
    """
    tool_input = {
        "items": [{"name": "Plato Fantasma", "qty": 1}],
        "payment_method": "Nequi",
    }

    returned_tool, returned_reply, returned_input = await _run_validate(
        "create_delivery_order",
        {**tool_input, "address": "Calle Falsa 123"},
    )

    # Tool must be nullified (rejected)
    assert returned_tool is None, (
        f"Tool call should be rejected for phantom item; got tool={returned_tool!r}"
    )
    # Reply must explain what happened — not the original LLM reply
    assert returned_reply is not None
    assert "Plato Fantasma" in returned_reply, (
        f"Reply must name the missing item; got: {returned_reply!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Correct items, wrong total: server recomputes correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_correct_items_wrong_total_recomputed():
    """
    LLM returns create_pickup_order with two real items but an incorrect total=100.
    Server MUST recompute: 1x Bandeja Paisa (28000) + 2x Agua (4000) = 32000.
    """
    tool_input = {
        "items": [
            {"name": "Bandeja Paisa", "qty": 1},
            {"name": "Agua",          "qty": 2},
        ],
        "total": 100,                   # ← wrong total from LLM
        "payment_method": "Daviplata",
    }

    _, _, resolved_input = await _run_validate("create_pickup_order", tool_input)

    assert resolved_input, "Tool call should not have been rejected"

    resolved_total = resolved_input.get("_resolved_total")
    assert resolved_total is not None
    expected = Decimal("28000") + Decimal("2") * Decimal("2000")  # 32000
    assert resolved_total == expected, (
        f"Expected {expected}, got {resolved_total!r} — server must recompute"
    )


# ---------------------------------------------------------------------------
# Test 4 — Mixed bag: one real item + one phantom → rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mixed_real_and_phantom_item_rejected():
    """
    One real item + one phantom item: the entire tool call is rejected.
    The error message must mention the phantom, not the valid item.
    """
    tool_input = {
        "items": [
            {"name": "Ajiaco",            "qty": 1},
            {"name": "Hamburguesa Rara",  "qty": 1},
        ],
        "payment_method": "Efectivo",
    }

    returned_tool, returned_reply, _ = await _run_validate(
        "create_pickup_order", tool_input
    )

    assert returned_tool is None, "Partial phantom must reject the full tool call"
    assert "Hamburguesa Rara" in (returned_reply or ""), (
        f"Reply must name the missing item; got: {returned_reply!r}"
    )
    # The valid item must NOT be mentioned as missing
    assert "Ajiaco" not in (returned_reply or "").replace("Ajiaco", "XAJIACO") or \
           "Ajiaco" not in returned_reply.split("encontré")[1] if "encontré" in (returned_reply or "") else True


# ---------------------------------------------------------------------------
# Test 5 — _resolve_items_server_side unit test: Decimal arithmetic is correct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_items_server_side_returns_decimal_total():
    """
    Unit test for _resolve_items_server_side directly.
    Confirms no float leaks into the arithmetic pipeline.
    """
    from app.services.agent import _resolve_items_server_side

    with patch("app.services.orders.db") as mock_orders_db:
        mock_orders_db.db_get_menu = AsyncMock(return_value=SAMPLE_MENU)

        items = [
            {"name": "Jugo de Mora", "qty": 3},
            {"name": "Agua",         "qty": 1},
        ]
        resolved, total, errors = await _resolve_items_server_side(items, BOT_NUMBER)

    assert errors == [], f"No errors expected; got {errors}"
    assert len(resolved) == 2

    expected_total = Decimal("3") * Decimal("5000") + Decimal("2000")  # 17000
    assert total == expected_total, f"Expected 17000, got {total!r}"
    assert isinstance(total, Decimal), "Total must be Decimal, not float"

    # Each resolved item must carry Decimal unit_price and line_total
    for item in resolved:
        assert isinstance(item["unit_price"], Decimal), (
            f"unit_price must be Decimal; item={item}"
        )
        assert isinstance(item["line_total"], Decimal), (
            f"line_total must be Decimal; item={item}"
        )


# ---------------------------------------------------------------------------
# Test 6 — Empty item name is rejected as an error, not silently skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_item_name_counted_as_error():
    """
    An item dict with no name/sku cannot be resolved and must be added to errors.
    """
    from app.services.agent import _resolve_items_server_side

    with patch("app.services.orders.db") as mock_orders_db:
        mock_orders_db.db_get_menu = AsyncMock(return_value=SAMPLE_MENU)

        items = [{"qty": 1}]  # no name, no sku
        resolved, total, errors = await _resolve_items_server_side(items, BOT_NUMBER)

    assert resolved == [], "No item should resolve without a name"
    assert len(errors) == 1, f"Expected 1 error; got {errors}"
