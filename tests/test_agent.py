"""
Suite 2 — AI Engine: System Prompt & Module Restrictions
tests/test_agent.py

Tests the "brain" of Mesio without making any real Claude API calls.

Covers:
  1.  _build_module_restrictions: all modules enabled → empty string (no block injected)
  2.  _build_module_restrictions: one module explicitly False → restriction present
  3.  _build_module_restrictions: multiple modules False → multiple restrictions
  4.  _build_module_restrictions: absent key (opt-out) treated as enabled → no restriction
  5.  _build_module_restrictions: none=None input → empty string
  6.  build_system_prompt: no disabled modules → 1 block (cached static only)
  7.  build_system_prompt: one disabled module → 2 blocks (static + restriction)
  8.  build_system_prompt: Block 0 always carries cache_control=ephemeral
  9.  build_system_prompt: Block 1 (restriction) never carries cache_control
 10.  execute_action: action="order" WITHOUT table context → blocked (returns URL hint)
 11.  execute_action: action="order" WITH table context → proceeds to order creation
 12.  execute_action: action="chat" → reply returned verbatim, no DB calls
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _features(**overrides) -> dict:
    """Build a features dict; unspecified modules default to True (opt-out model)."""
    base = {
        "module_reservations": True,
        "module_orders":       True,
        "module_tables":       True,
        "staff_tips":          True,
        "loyalty":             True,
    }
    base.update(overrides)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# 1–5. _build_module_restrictions  (pure function — no mocking needed)
# ══════════════════════════════════════════════════════════════════════════════

def test_module_restrictions_all_enabled():
    """All modules True → no restriction block generated."""
    from app.services.agent import _build_module_restrictions
    result = _build_module_restrictions(_features())
    assert result == ""


def test_module_restrictions_reservations_off():
    """reservations=False → block contains the module name and action prohibition."""
    from app.services.agent import _build_module_restrictions
    result = _build_module_restrictions(_features(module_reservations=False))

    assert result != ""
    assert "RESTRICCIONES DE MÓDULOS INACTIVOS" in result
    assert "Reservaciones" in result
    # The forbidden action for reservations is "reserve"
    assert 'action="reserve"' in result


def test_module_restrictions_orders_off():
    """module_orders=False → delivery and pickup actions must be forbidden."""
    from app.services.agent import _build_module_restrictions
    result = _build_module_restrictions(_features(module_orders=False))

    assert 'action="delivery"' in result
    assert 'action="pickup"' in result


def test_module_restrictions_multiple_off():
    """Two disabled modules → two restriction paragraphs."""
    from app.services.agent import _build_module_restrictions
    result = _build_module_restrictions(_features(
        module_reservations=False,
        module_orders=False,
    ))

    assert result.count("RESTRICCIÓN ACTIVA") == 2


def test_module_restrictions_absent_key_is_enabled():
    """
    Absent key in features → treated as enabled (opt-out model).
    No restriction must be added for the absent module.
    """
    from app.services.agent import _build_module_restrictions
    # module_reservations key is completely absent
    result = _build_module_restrictions({"module_orders": True})
    assert result == ""


def test_module_restrictions_none_input():
    """None input → empty string, no crash."""
    from app.services.agent import _build_module_restrictions
    assert _build_module_restrictions(None) == ""


def test_module_restrictions_empty_dict():
    """Empty dict → no modules disabled → empty string."""
    from app.services.agent import _build_module_restrictions
    assert _build_module_restrictions({}) == ""


def test_module_restrictions_staff_tips_no_action_prohibition():
    """
    staff_tips has no forbidden actions in _MODULE_RULES (it's UI-only).
    Restriction text should still mention the module but omit the action clause.
    """
    from app.services.agent import _build_module_restrictions
    result = _build_module_restrictions(_features(staff_tips=False))

    assert "RESTRICCIÓN ACTIVA" in result
    # No action=".." clause expected for staff_tips
    assert 'action=' not in result


# ══════════════════════════════════════════════════════════════════════════════
# 6–9. build_system_prompt  (async, no Claude API call)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_build_system_prompt_no_restrictions():
    """All modules enabled → prompt list has exactly 1 block."""
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(_features())
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_build_system_prompt_with_restriction():
    """Disabled module → prompt list has 2 blocks."""
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(_features(module_reservations=False))
    assert len(blocks) == 2


@pytest.mark.asyncio
async def test_build_system_prompt_none_features():
    """None features → 1 block only (no restrictions)."""
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(None)
    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_build_system_prompt_block0_has_cache_control():
    """Block 0 always carries cache_control=ephemeral for prompt caching."""
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(_features())
    block0 = blocks[0]
    assert block0.get("cache_control") == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_build_system_prompt_block1_no_cache_control():
    """
    Block 1 (restriction block) must NOT carry cache_control —
    it is per-restaurant dynamic content and must not be cached.
    """
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(_features(module_reservations=False))
    assert len(blocks) == 2
    block1 = blocks[1]
    assert "cache_control" not in block1


@pytest.mark.asyncio
async def test_build_system_prompt_block1_contains_restriction():
    """Block 1 text contains the injected restriction content."""
    from app.services.agent import build_system_prompt
    blocks = await build_system_prompt(_features(module_orders=False))
    restriction_text = blocks[1]["text"]
    assert "RESTRICCIONES DE MÓDULOS INACTIVOS" in restriction_text
    assert 'action="delivery"' in restriction_text


# ══════════════════════════════════════════════════════════════════════════════
# 10–12. execute_action  (critical business-rule tests)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execute_action_order_without_table_is_blocked():
    """
    action="order" with no table_context must be blocked.
    The return message must NOT confirm the order — it must ask for the table.
    This is the "Protección Antifantasmas" guard.
    """
    from app.services import agent

    parsed = {
        "action": "order",
        "items":  [{"name": "Hamburguesa", "qty": 1}],
        "reply":  "Perfecto, tu pedido está listo.",
    }

    # add_to_cart runs in the item pre-processing loop BEFORE the table check,
    # so it will be called. We mock it to return a valid success dict.
    # What must NOT happen is db_save_table_order being called.
    with (
        patch.object(agent.orders, "add_to_cart",
                     AsyncMock(return_value={"success": True, "dish": {"name": "Hamburguesa"}})),
        patch.object(agent.db, "db_save_table_order", AsyncMock()) as mock_save_order,
    ):
        result = await agent.execute_action(
            parsed=parsed,
            phone="573001234567",
            bot_number="+573009999999",
            table_context=None,      # ← no table context
            session_state={},
        )

    # The reply must signal that a table is required, not confirm an order
    assert isinstance(result, str)
    low = result.lower()
    assert any(kw in low for kw in ["mesa", "domicilio", "recoger", "catalog"])
    # No table order must have been persisted
    mock_save_order.assert_not_called()


@pytest.mark.asyncio
async def test_execute_action_order_with_table_proceeds():
    """
    action="order" WITH table_context must proceed to create the order.
    We verify that db_get_cart is consulted and create_table_order is called.
    """
    from app.services import agent

    parsed = {
        "action": "order",
        "items":  [{"name": "Pizza", "qty": 2}],
        "reply":  "Tu pedido fue enviado a cocina.",
        "notes":  "",
        "separate_bill": False,
    }

    table_ctx = {"id": "table-uuid-1", "name": "Mesa 3"}

    mock_cart_data = {"items": [{"name": "Pizza", "quantity": 2, "price": 35000, "subtotal": 70000}]}

    with (
        patch.object(agent.orders, "add_to_cart",
                     AsyncMock(return_value={"success": True, "dish": {"name": "Pizza"}})),
        patch.object(agent.orders, "get_cart_total",         AsyncMock(return_value=70000)),
        patch.object(agent.orders, "clear_cart",             AsyncMock()),
        patch.object(agent.db,     "db_get_cart",            AsyncMock(return_value=mock_cart_data)),
        patch.object(agent.db,     "db_get_base_order_id",   AsyncMock(return_value="BASE-001")),
        patch.object(agent.db,     "db_get_next_sub_number", AsyncMock(return_value=1)),
        patch.object(agent.db,     "db_save_table_order",    AsyncMock()) as mock_save_order,
        patch.object(agent.db,     "db_session_mark_order",  AsyncMock()),
        patch.object(agent.db,     "db_deduct_inventory_for_order", AsyncMock()),
        patch.object(agent.db,     "db_get_restaurant_by_bot_number",
                     AsyncMock(return_value={"features": {}})),
        # Patch at the pool level so no real DB is touched for the cart DELETE
        patch.object(agent.db,     "get_pool",
                     AsyncMock(return_value=AsyncMock(
                         acquire=__import__('unittest.mock', fromlist=['MagicMock']).MagicMock(
                             return_value=AsyncMock(
                                 __aenter__=AsyncMock(return_value=AsyncMock()),
                                 __aexit__=AsyncMock(return_value=False),
                             )
                         )
                     ))),
    ):
        result = await agent.execute_action(
            parsed=parsed,
            phone="573001234567",
            bot_number="+573009999999",
            table_context=table_ctx,
            session_state={},
        )

    # Result must NOT be the "needs a table" blocked message
    assert isinstance(result, str)
    assert "Para tomar tu pedido, necesito saber en qué mesa" not in result
    # At least one table_order must have been saved
    mock_save_order.assert_called_once()


@pytest.mark.asyncio
async def test_execute_action_chat_returns_reply():
    """
    action="chat" must return the reply unchanged without touching the DB.
    """
    from app.services import agent

    parsed = {"action": "chat", "reply": "¡Hola! ¿En qué te puedo ayudar?", "items": []}

    with (
        patch.object(agent.db, "db_get_cart", AsyncMock()) as mock_db,
    ):
        result = await agent.execute_action(
            parsed=parsed,
            phone="573001234567",
            bot_number="+573009999999",
            table_context=None,
            session_state={},
        )

    assert result == parsed["reply"]
    mock_db.assert_not_called()
