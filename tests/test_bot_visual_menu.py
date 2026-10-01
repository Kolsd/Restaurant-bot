"""
tests/test_bot_visual_menu.py

Test suite for the bot's visual menu (send_dish_card → a dish card in the web chat).

Covers:
  - _validate_tool_call gate: flag off / flag on + image / flag on + no image / dish not found
  - execute_action: pushes a dish_cards block and keeps the LLM reply

Patterns follow existing suite conventions (no pytest.mark.asyncio — uses
asyncio.get_event_loop().run_until_complete or plain sync where possible).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.services.agent import _validate_tool_call, execute_action, _build_compact_menu
from app.services import state_store


# ── Helpers ───────────────────────────────────────────────────────────────────

_BOT = "+573009876543"
_PHONE = "573001234567"
_DISH_WITH_IMAGE = {
    "name": "Bandeja Paisa",
    "description": "Fríjoles, chicharrón, carne molida y más",
    "price": 28000,
    "image_url": "https://res.cloudinary.com/mesio/image/upload/v1/mesio/r_1/dish_abc.webp",
    "image_public_id": "mesio/r_1/dish_abc",
    "active": True,
}
_DISH_NO_IMAGE = {
    "name": "Arroz Blanco",
    "description": "Arroz de grano largo",
    "price": 5000,
    "image_url": None,
    "active": True,
}

_MENU = {"Platos": [_DISH_WITH_IMAGE, _DISH_NO_IMAGE]}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Section A — _validate_tool_call gate ─────────────────────────────────────

class TestValidateToolCallSendDishCard:
    """Validation guard for send_dish_card in _validate_tool_call."""

    def test_flag_off_rejects(self, monkeypatch):
        """When bot_visual_menu flag is off, validation downgrades to chat (None tool)."""
        features = {"bot_visual_menu": False}
        tool_input = {"dish_name": "Bandeja Paisa"}

        with patch("app.services.orders.find_dish", new=AsyncMock(return_value=_DISH_WITH_IMAGE)):
            result_name, result_reply, result_input = _run(
                _validate_tool_call(
                    "send_dish_card", tool_input, "Aquí te muestro:", None,
                    _BOT, _PHONE, features=features,
                )
            )

        assert result_name is None, "tool should be nullified when flag is off"

    def test_flag_on_dish_with_image_passes(self, monkeypatch):
        """When flag is on and dish has image_url, validation passes and injects _resolved_dish."""
        features = {"bot_visual_menu": True}
        tool_input = {"dish_name": "Bandeja Paisa"}

        with patch("app.services.orders.find_dish", new=AsyncMock(return_value=_DISH_WITH_IMAGE)):
            tool_name, reply, ti = _run(
                _validate_tool_call(
                    "send_dish_card", tool_input, "Mira:", None,
                    _BOT, _PHONE, features=features,
                )
            )

        assert tool_name == "send_dish_card"
        assert ti.get("_resolved_dish") == _DISH_WITH_IMAGE

    def test_flag_on_dish_no_image_rejects(self, monkeypatch):
        """When flag is on but dish has no image_url, validation downgrades to chat."""
        features = {"bot_visual_menu": True}
        tool_input = {"dish_name": "Arroz Blanco"}

        with patch("app.services.orders.find_dish", new=AsyncMock(return_value=_DISH_NO_IMAGE)):
            tool_name, reply, ti = _run(
                _validate_tool_call(
                    "send_dish_card", tool_input, "Aquí te muestro:", None,
                    _BOT, _PHONE, features=features,
                )
            )

        assert tool_name is None

    def test_dish_not_in_menu_rejects(self, monkeypatch):
        """When find_dish returns None (dish not found), validation downgrades to chat."""
        features = {"bot_visual_menu": True}
        tool_input = {"dish_name": "Plato Inexistente"}

        with patch("app.services.orders.find_dish", new=AsyncMock(return_value=None)):
            tool_name, reply, ti = _run(
                _validate_tool_call(
                    "send_dish_card", tool_input, "Aquí:", None,
                    _BOT, _PHONE, features=features,
                )
            )

        assert tool_name is None

    def test_tool_input_not_dict_rejects(self, monkeypatch):
        """Non-dict tool_input (e.g. SDK object) is rejected safely."""
        features = {"bot_visual_menu": True}
        not_a_dict = MagicMock()
        not_a_dict.__class__ = object  # ensure not isinstance(x, dict) is True
        # Replace with a plain non-dict value
        tool_name, reply, ti = _run(
            _validate_tool_call(
                "send_dish_card", "not a dict", "reply", None,
                _BOT, _PHONE, features=features,
            )
        )
        assert tool_name is None

    def test_dish_name_too_long_rejects(self, monkeypatch):
        """dish_name exceeding 200 chars is rejected."""
        features = {"bot_visual_menu": True}
        tool_input = {"dish_name": "A" * 201}

        with patch("app.services.orders.find_dish", new=AsyncMock(return_value=_DISH_WITH_IMAGE)):
            tool_name, reply, ti = _run(
                _validate_tool_call(
                    "send_dish_card", tool_input, "reply", None,
                    _BOT, _PHONE, features=features,
                )
            )

        assert tool_name is None


# ── Section B — execute_action handler ───────────────────────────────────────

def _run_turn(coro):
    """Run one agent turn with its block bucket open; return (result, blocks)."""
    from app.services import blocks

    async def _go():
        token = blocks.begin_turn()
        try:
            result = await coro
            return result, blocks.drain_blocks()
        finally:
            blocks.end_turn(token)

    return _run(_go())


class TestExecuteActionSendDishCard:
    """send_dish_card shows the dish's card — photo, price — in the web chat.
    (It sent the photo over WhatsApp until 2026-09-25.)"""

    def _make_parsed(self, reply="Aquí tienes:"):
        return {
            "action": "send_dish_card",
            "reply": reply,
            "dish_name": "Bandeja Paisa",
            "caption": "",
            "_resolved_dish": _DISH_WITH_IMAGE,
        }

    def _restaurant_obj(self, currency="COP"):
        return {
            "id": 1,
            "name": "Test Rest",
            "features": {"bot_visual_menu": True, "currency": currency},
        }

    def test_pushes_the_dish_card_and_keeps_the_reply(self):
        result, pushed = _run_turn(execute_action(
            self._make_parsed(), _PHONE, _BOT, None, {},
            restaurant_obj=self._restaurant_obj(),
        ))
        assert result == "Aquí tienes:"
        cards = [b for b in pushed if b.get("type") == "dish_cards"]
        assert len(cards) == 1
        (dish,) = cards[0]["dishes"]
        assert dish["name"] == "Bandeja Paisa"
        assert dish["image_url"] == _DISH_WITH_IMAGE["image_url"]
        assert dish["price"] == 28000

    def test_without_llm_text_still_says_something(self):
        result, pushed = _run_turn(execute_action(
            self._make_parsed(reply=""), _PHONE, _BOT, None, {},
            restaurant_obj=self._restaurant_obj(),
        ))
        assert result == "Aquí tienes Bandeja Paisa."
        assert any(b.get("type") == "dish_cards" for b in pushed)

    def test_no_resolved_dish_pushes_nothing(self):
        parsed = self._make_parsed()
        parsed["_resolved_dish"] = {}
        result, pushed = _run_turn(execute_action(
            parsed, _PHONE, _BOT, None, {}, restaurant_obj=self._restaurant_obj(),
        ))
        assert result == "Aquí tienes:"
        assert not [b for b in pushed if b.get("type") == "dish_cards"]


class TestBuildCompactMenuPhotoMarkers:
    """_build_compact_menu marks dishes with [📷] only when bot_visual_menu=True and image_url set."""

    def test_flag_off_no_photo_markers(self):
        menu = {"Platos": [_DISH_WITH_IMAGE]}
        result = _build_compact_menu(menu, {}, bot_visual_menu=False)
        assert "[📷]" not in result

    def test_flag_on_dish_with_image_has_marker(self):
        menu = {"Platos": [_DISH_WITH_IMAGE]}
        result = _build_compact_menu(menu, {}, bot_visual_menu=True)
        assert "[📷]" in result

    def test_flag_on_dish_without_image_no_marker(self):
        menu = {"Platos": [_DISH_NO_IMAGE]}
        result = _build_compact_menu(menu, {}, bot_visual_menu=True)
        assert "[📷]" not in result

    def test_mixed_menu_only_image_dishes_marked(self):
        menu = {"Platos": [_DISH_WITH_IMAGE, _DISH_NO_IMAGE]}
        result = _build_compact_menu(menu, {}, bot_visual_menu=True)
        lines = result.split("\n")
        assert len(lines) == 1  # single category
        # Bandeja Paisa has [📷], Arroz Blanco does not
        assert "Bandeja Paisa [📷]" in result
        assert "Arroz Blanco [📷]" not in result


# ── Section D — send_dish_card available in EXTERNAL flow ────────────────────


class TestSendDishCardExternalFlow:
    """send_dish_card MUST be exposed in TOOLS_SALON so a customer can ask
    'cómo se ve la bandeja paisa' and receive a photo, whether they're at a
    table or (before chunk 9, docs/claude/delivery-web.md) using the retired
    WhatsApp delivery/pickup funnel. TOOLS_SALON is the only tool list left
    since that funnel's TOOLS_EXTERNAL was deleted.

    The handler in agent.py is flow-agnostic (no salon-only filtering on the
    tool name), so being present in TOOLS_SALON is sufficient — exercising
    execute_action with table_context=None (no active table) confirms parity.
    """

    def test_tool_definition_present_in_external(self):
        from app.services.agent_tools import TOOLS_SALON, ALL_TOOLS

        names = [t["name"] for t in TOOLS_SALON]
        assert "send_dish_card" in names, "send_dish_card MUST be in TOOLS_SALON"

        # Same definition should resolve from ALL_TOOLS lookup.
        assert ALL_TOOLS["send_dish_card"]["name"] == "send_dish_card"
        assert "dish_name" in ALL_TOOLS["send_dish_card"]["input_schema"]["properties"]

    def test_without_a_table_the_card_is_shown_too(self):
        """table_context=None (the web ordering chat) — the handler is
        flow-agnostic and pushes the same card."""
        parsed = {
            "action": "send_dish_card",
            "reply": "Te muestro la bandeja:",
            "dish_name": "Bandeja Paisa",
            "caption": "",
            "_resolved_dish": _DISH_WITH_IMAGE,
        }
        result, pushed = _run_turn(execute_action(
            parsed, _PHONE, _BOT, None, {},
            restaurant_obj={"id": 1, "name": "Test Rest", "features": {"bot_visual_menu": True}},
        ))
        assert result == "Te muestro la bandeja:"
        assert any(b.get("type") == "dish_cards" for b in pushed)
