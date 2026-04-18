"""
tests/test_inbox_worker_org_routing.py

Unit tests for Bloque S4 inbox_worker changes:
  - _handle_meta_whatsapp routes via db_get_org_by_phone (not db_get_restaurant_by_phone)
  - _extract_location_from_payload parses QR deep-link formats
  - Location validation rejects location_ids from other Orgs
  - Org-not-found acks without retry (log + return)

Tests:
  1. test_webhook_routes_to_org_by_phone
  2. test_webhook_extracts_location_from_qr_payload
  3. test_webhook_location_override_populates_location_id
  4. test_webhook_invalid_location_id_is_ignored
  5. test_webhook_no_org_match_mark_processed
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_org(org_id: int = 42, wa_access_token: str = "tok_test") -> dict:
    return {
        "id": org_id,
        "name": "Test Org",
        "whatsapp_number": "573001234567",
        "wa_phone_id": "phone_id_1",
        "wa_access_token": wa_access_token,
        "menu": {},
        "features": {},
        "matched_location_id": None,
    }


def _make_payload(
    bot_number: str = "573001234567",
    user_phone: str = "573109999999",
    user_text: str = "Hola",
    phone_id: str = "phone_id_1",
    **extras,
) -> dict:
    base = {
        "bot_number": bot_number,
        "user_phone": user_phone,
        "user_text": user_text,
        "phone_id": phone_id,
    }
    base.update(extras)
    return base


# ── Test 1: Routes via db_get_org_by_phone ─────────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_routes_to_org_by_phone():
    """inbox_worker uses db_get_org_by_phone to resolve the Org, NOT db_get_restaurant_by_phone.
    _process_message is called with the Org dict as restaurant, wrapped in tenant_scope(org_id).
    """
    org = _make_org(org_id=42)
    payload = _make_payload()

    process_message_mock = AsyncMock()

    with (
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=org),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=None),
        ),
        patch("app.routes.chat._process_message", process_message_mock),
    ):
        from app.services.inbox_worker import _handle_meta_whatsapp
        await _handle_meta_whatsapp(payload)

    # _process_message MUST have been called
    process_message_mock.assert_called_once()
    call_kwargs = process_message_mock.call_args.kwargs
    assert call_kwargs["user_phone"] == "573109999999"
    assert call_kwargs["bot_number"] == "573001234567"
    assert call_kwargs["access_token"] == "tok_test"
    # location_id None — no QR in this payload
    assert call_kwargs["location_id"] is None


# ── Test 2: Extracts location_id from QR deep-link ───────────────────────────

@pytest.mark.asyncio
async def test_webhook_extracts_location_from_qr_payload():
    """Payload with 'mesio://loc/10' in user_text resolves to location_id=10.
    The location is validated against the Org before use.
    """
    org = _make_org(org_id=42)
    # Location belongs to org 42 — valid
    loc = {"id": 10, "org_id": 42, "name": "Sede Norte"}
    payload = _make_payload(user_text="mesio://loc/10")

    process_message_mock = AsyncMock()

    with (
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=org),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=loc),
        ),
        patch("app.routes.chat._process_message", process_message_mock),
    ):
        from app.services.inbox_worker import _handle_meta_whatsapp
        await _handle_meta_whatsapp(payload)

    call_kwargs = process_message_mock.call_args.kwargs
    assert call_kwargs["location_id"] == 10


# ── Test 3: matched_location_id from Location phone override ─────────────────

@pytest.mark.asyncio
async def test_webhook_location_override_populates_location_id():
    """When db_get_org_by_phone returns matched_location_id (Location phone match),
    _process_message receives that location_id.
    """
    org = _make_org(org_id=5)
    org["matched_location_id"] = 77  # phone matched a specific Location

    payload = _make_payload(user_text="Quiero un pedido")

    process_message_mock = AsyncMock()

    with (
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=org),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=None),  # no QR
        ),
        patch("app.routes.chat._process_message", process_message_mock),
    ):
        from app.services.inbox_worker import _handle_meta_whatsapp
        await _handle_meta_whatsapp(payload)

    call_kwargs = process_message_mock.call_args.kwargs
    assert call_kwargs["location_id"] == 77


# ── Test 4: Invalid QR location_id (different Org) is ignored ────────────────

@pytest.mark.asyncio
async def test_webhook_invalid_location_id_is_ignored():
    """QR payload says loc=999, but that Location belongs to org_id=99, not org_id=42.
    The invalid location_id should be discarded; matched_location_id fallback is used.
    """
    org = _make_org(org_id=42)
    org["matched_location_id"] = 10  # valid phone-override fallback

    payload = _make_payload(user_text="mesio://loc/999")

    # Location 999 belongs to a different org (99)
    foreign_loc = {"id": 999, "org_id": 99, "name": "Sede Extraña"}

    process_message_mock = AsyncMock()

    with (
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=org),
        ),
        patch(
            "app.repositories.restaurant_repo.db_get_location_by_id",
            AsyncMock(return_value=foreign_loc),
        ),
        patch("app.routes.chat._process_message", process_message_mock),
    ):
        from app.services.inbox_worker import _handle_meta_whatsapp
        await _handle_meta_whatsapp(payload)

    call_kwargs = process_message_mock.call_args.kwargs
    # QR location discarded; falls back to matched_location_id=10
    assert call_kwargs["location_id"] == 10


# ── Test 5: Org not found — ack without retry ────────────────────────────────

@pytest.mark.asyncio
async def test_webhook_no_org_match_mark_processed():
    """When db_get_org_by_phone returns None, the handler returns (acks) immediately
    without calling _process_message — no retry loop.
    """
    payload = _make_payload(bot_number="573000000000")

    process_message_mock = AsyncMock()

    with (
        patch(
            "app.repositories.restaurant_repo.db_get_org_by_phone",
            AsyncMock(return_value=None),
        ),
        patch("app.routes.chat._process_message", process_message_mock),
    ):
        from app.services.inbox_worker import _handle_meta_whatsapp
        await _handle_meta_whatsapp(payload)

    # _process_message MUST NOT be called — ack without dispatch
    process_message_mock.assert_not_called()


# ── Test 6: _extract_location_from_payload — interactive button id ────────────

def test_extract_location_from_interactive_button():
    """button_id='loc:15' should resolve to location_id=15."""
    from app.services.inbox_worker import _extract_location_from_payload
    payload = {"button_id": "loc:15", "user_text": ""}
    assert _extract_location_from_payload(payload) == 15


def test_extract_location_from_text_deeplink():
    """user_text with 'mesio://loc/7' should resolve to location_id=7."""
    from app.services.inbox_worker import _extract_location_from_payload
    payload = {"user_text": "Aquí está mesio://loc/7 mi ubicación"}
    assert _extract_location_from_payload(payload) == 7


def test_extract_location_from_context_field():
    """context.location_id field should resolve directly."""
    from app.services.inbox_worker import _extract_location_from_payload
    payload = {"context": {"location_id": 99}, "user_text": ""}
    assert _extract_location_from_payload(payload) == 99


def test_extract_location_returns_none_when_absent():
    """No location marker → returns None."""
    from app.services.inbox_worker import _extract_location_from_payload
    payload = {"user_text": "Quiero una bandeja", "button_id": "skip_nps"}
    assert _extract_location_from_payload(payload) is None


def test_extract_location_context_takes_priority_over_text():
    """context.location_id has higher priority than text deep-link."""
    from app.services.inbox_worker import _extract_location_from_payload
    payload = {"context": {"location_id": 5}, "user_text": "mesio://loc/9"}
    # context wins (checked first in implementation)
    assert _extract_location_from_payload(payload) == 5
