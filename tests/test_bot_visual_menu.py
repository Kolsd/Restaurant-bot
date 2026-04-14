"""
tests/test_bot_visual_menu.py

Test suite for Catalog v2 — Fase 4: WhatsApp bot visual menu (send_dish_card).

Covers:
  - _validate_tool_call gate: flag off / flag on + image / flag on + no image / dish not found
  - execute_action: image sent successfully
  - execute_action: send_image raises → fallback text via meta_api.send_text
  - Rate limiting: 4th request in window → fallback text (no image sent)

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

class TestExecuteActionSendDishCard:
    """execute_action handler for send_dish_card action."""

    def _make_parsed(self, caption=""):
        return {
            "action": "send_dish_card",
            "reply": "Aquí tienes:",
            "dish_name": "Bandeja Paisa",
            "caption": caption,
            "_resolved_dish": _DISH_WITH_IMAGE,
        }

    def _restaurant_obj(self):
        return {
            "id": 1,
            "name": "Test Rest",
            "wa_access_token": "token_abc",
            "wa_phone_id": "pid_001",
            "features": {"bot_visual_menu": True},
        }

    def test_image_sent_successfully_returns_reply(self, monkeypatch):
        """When send_image succeeds, execute_action returns the LLM reply."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=True)
        )

        send_image_mock = AsyncMock(return_value=True)

        with patch("app.services.meta_api.send_image", send_image_mock):
            result = _run(
                execute_action(
                    self._make_parsed(),
                    _PHONE, _BOT, None, {},
                    restaurant_obj=self._restaurant_obj(),
                )
            )

        assert result == "Aquí tienes:"
        send_image_mock.assert_awaited_once()

    def test_send_image_raises_uses_fallback_text(self, monkeypatch):
        """When send_image raises an exception, fallback text is returned (Regla 8)."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=True)
        )

        send_image_mock = AsyncMock(side_effect=Exception("network error"))

        with patch("app.services.meta_api.send_image", send_image_mock):
            result = _run(
                execute_action(
                    self._make_parsed(),
                    _PHONE, _BOT, None, {},
                    restaurant_obj=self._restaurant_obj(),
                )
            )

        # Should fall back to text — not empty, not raising
        assert result  # non-empty string
        assert "Bandeja Paisa" in result or result == "Aquí tienes:"

    def test_send_image_returns_false_uses_fallback_text(self, monkeypatch):
        """When send_image returns False, fallback text is returned (Regla 8)."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=True)
        )

        send_image_mock = AsyncMock(return_value=False)

        with patch("app.services.meta_api.send_image", send_image_mock):
            result = _run(
                execute_action(
                    self._make_parsed(),
                    _PHONE, _BOT, None, {},
                    restaurant_obj=self._restaurant_obj(),
                )
            )

        # Must not be empty — fallback text contains dish info
        assert result
        assert "Bandeja Paisa" in result or result == "Aquí tienes:"

    def test_rate_limited_returns_fallback_no_image_call(self, monkeypatch):
        """4th request in 60s window → rate_limit_check returns False → no image sent."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=False)
        )

        send_image_mock = AsyncMock(return_value=True)

        with patch("app.services.meta_api.send_image", send_image_mock):
            result = _run(
                execute_action(
                    self._make_parsed(),
                    _PHONE, _BOT, None, {},
                    restaurant_obj=self._restaurant_obj(),
                )
            )

        send_image_mock.assert_not_awaited()
        assert result  # Fallback text returned, not empty

    def test_no_access_token_uses_fallback(self, monkeypatch):
        """When no access_token is available, fallback text is returned without calling send_image."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=True)
        )
        monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)

        rest = {
            "id": 1, "name": "Test Rest",
            "wa_access_token": None, "wa_phone_id": None,
            "features": {"bot_visual_menu": True},
        }

        send_image_mock = AsyncMock(return_value=True)

        with patch("app.services.meta_api.send_image", send_image_mock):
            result = _run(
                execute_action(
                    self._make_parsed(),
                    _PHONE, _BOT, None, {},
                    restaurant_obj=rest,
                )
            )

        send_image_mock.assert_not_awaited()
        assert result


# ── Section C — _build_compact_menu photo markers ─────────────────────────────

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
