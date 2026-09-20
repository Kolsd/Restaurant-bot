"""
tests/test_whatsapp_delivery_retired.py

Chunk 9 (docs/claude/delivery-web.md): WhatsApp delivery/pickup ordering was
retired. A WhatsApp customer with no active table session now gets a
deterministic, no-LLM reply pointing at the web ordering page instead of the
deleted "external" funnel (app/services/agent_external.py) — and it must be
structurally impossible for that reply path to create an order, since no LLM
call (and therefore no tool_use loop) ever happens for it.

Covers:
  1. A WhatsApp message with no table context gets the /pedir/{slug} link
     reply, and the LLM/order-creation path is never invoked.
  2. An org with no slug gets the no-link "call the restaurant" reply.
  3. GET /api/delivery/orders (deleted org-wide endpoint) is gone (404).
  4. Nothing under app/static/** references the deleted legacy
     /api/delivery/orders* routes any more.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.conftest import make_pool


APP_STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

_LEGACY_DELIVERY_PATTERNS = (
    "/api/delivery/orders",
    "/api/delivery/check-updates",
)


def _patch_no_table_chat(monkeypatch, restaurant: dict):
    """Minimum DB mocks for agent.chat() to reach detect_table_context's
    "no active table, no QR claim" outcome (table_context=None) without a
    real DB. Mirrors the pattern in test_customer_memory_integration.py /
    test_full_flow.py's _patch_db_for_chat helpers.
    """
    from app.services import database as db

    monkeypatch.setattr(db, "db_get_restaurant_by_bot_number",
                         AsyncMock(return_value=restaurant))
    monkeypatch.setattr(db, "db_get_active_session", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.agent.state_store.nps_get", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.agent.state_store.checkout_get", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.agent.state_store.join_code_pending_get", AsyncMock(return_value=None))

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    monkeypatch.setattr(db, "get_pool", AsyncMock(return_value=make_pool(conn)))


# ── 1. Real WhatsApp phone, no table → deterministic link reply, no LLM ──────

@pytest.mark.asyncio
async def test_whatsapp_no_table_message_gets_deterministic_link_reply(monkeypatch):
    import app.services.agent as agent_mod

    restaurant = {
        "id": 1, "org_id": 1, "name": "Restaurante Test",
        "whatsapp_number": "+573009876543", "slug": "restaurante-test",
        "features": {},
    }
    _patch_no_table_chat(monkeypatch, restaurant)

    # The LLM must NEVER be called for this path — assert on the call_claude
    # mock itself rather than on a reply string, so a future prompt-text
    # change can't accidentally make this test pass for the wrong reason.
    llm_mock = AsyncMock(return_value={"reply": "SHOULD NOT BE CALLED", "tool_name": None, "tool_input": {}})
    monkeypatch.setattr(agent_mod, "call_claude", llm_mock)

    # Same guard on order creation — this tool no longer exists, but assert
    # directly on the underlying repo call too, belt-and-suspenders.
    create_order_mock = AsyncMock(return_value={"success": False, "error": "SHOULD NOT BE CALLED"})
    monkeypatch.setattr("app.services.orders.create_order", create_order_mock)

    result = await agent_mod.chat("+573001234567", "Quiero un domicilio", "+573009876543")

    assert isinstance(result, dict)
    assert "/pedir/restaurante-test" in result["message"], (
        f"Expected the /pedir/{{slug}} link in the reply, got: {result['message']!r}"
    )
    llm_mock.assert_not_called()
    create_order_mock.assert_not_called()


# ── 2. Org without a slug → no-link fallback reply ───────────────────────────

@pytest.mark.asyncio
async def test_whatsapp_no_table_message_no_slug_tells_customer_to_call(monkeypatch):
    import app.services.agent as agent_mod

    restaurant = {
        "id": 2, "org_id": 2, "name": "Sin Slug SAS",
        "whatsapp_number": "+573009876544", "slug": None,
        "features": {},
    }
    _patch_no_table_chat(monkeypatch, restaurant)
    monkeypatch.setattr(agent_mod, "call_claude", AsyncMock(side_effect=AssertionError(
        "LLM must not be called for a WhatsApp message with no table context"
    )))

    result = await agent_mod.chat("+573001234555", "Quiero pedir para recoger", "+573009876544")

    assert isinstance(result, dict)
    assert "/pedir/" not in result["message"], (
        f"No slug exists — the reply must not contain a broken /pedir/ link: {result['message']!r}"
    )
    assert "comunícate" in result["message"].lower() or "restaurante" in result["message"].lower()


# ── 3. The deleted org-wide endpoint is gone ─────────────────────────────────

def test_legacy_delivery_orders_endpoint_is_gone(client):
    """GET /api/delivery/orders used to return EVERY delivery order of the
    org (any sede, any courier, full customer PII) to any authenticated
    staff member. Deleted in chunk 9 — the route must not exist at all."""
    resp = client.get("/api/delivery/orders", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code in (404, 405), (
        f"Expected 404/405 (route removed), got {resp.status_code}: {resp.text}"
    )


def test_legacy_delivery_check_updates_endpoint_is_gone(client):
    resp = client.get("/api/delivery/check-updates", headers={"Authorization": "Bearer whatever"})
    assert resp.status_code in (404, 405), (
        f"Expected 404/405 (route removed), got {resp.status_code}: {resp.text}"
    )


def test_legacy_delivery_order_action_endpoints_are_gone(client):
    """validate / eta / status — the three deleted per-order action routes."""
    for path, method in (
        ("/api/delivery/orders/x/validate", "post"),
        ("/api/delivery/orders/x/eta", "post"),
        ("/api/delivery/orders/x/status", "patch"),
    ):
        resp = getattr(client, method)(path, json={}, headers={"Authorization": "Bearer whatever"})
        assert resp.status_code in (404, 405), (
            f"{method.upper()} {path} expected 404/405 (route removed), "
            f"got {resp.status_code}: {resp.text}"
        )


# ── 4. Nothing in app/static/** references the deleted routes any more ──────

def test_static_tree_has_no_legacy_delivery_orders_references():
    offenders: list[str] = []
    for path in APP_STATIC_DIR.rglob("*.js"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _LEGACY_DELIVERY_PATTERNS:
            if pattern in text:
                offenders.append(f"{path.relative_to(APP_STATIC_DIR)} references {pattern!r}")
    assert not offenders, (
        "app/static/** still references the deleted legacy delivery endpoints:\n"
        + "\n".join(offenders)
    )


# ── 7. Only DELIVERY intent is deflected — the rest of WhatsApp still works ──


@pytest.mark.asyncio
async def test_whatsapp_reservation_request_is_not_deflected(monkeypatch):
    """Retiring delivery must not take WhatsApp reservations with it.

    A customer books a table BEFORE arriving, so a reservation request always
    comes from a phone with no table session — the same condition as a
    delivery request. Deflecting every table-less WhatsApp message killed
    booking (and every other question) on the channel.

    The assertion is on the GATE, not on the whole conversation: with the
    restaurant context unavailable, a deflected message would still return the
    /pedir link, while a message that passes the gate continues into the normal
    flow and stops at its "number not configured" reply.
    """
    import app.services.agent as agent_mod

    restaurant = {
        "id": 3, "org_id": 3, "name": "Reserva Test",
        "whatsapp_number": "+573009876545", "slug": "reserva-test",
        "features": {},
    }
    _patch_no_table_chat(monkeypatch, restaurant)
    monkeypatch.setattr(agent_mod, "_load_restaurant_context", AsyncMock(return_value=None))

    deflected = await agent_mod.chat(
        "+573001234777", "Quiero un domicilio", "+573009876545",
    )
    assert "/pedir/reserva-test" in deflected["message"], "control: delivery IS deflected"

    result = await agent_mod.chat(
        "+573001234777", "Quiero reservar una mesa para 4 el sábado", "+573009876545",
    )
    assert "/pedir/" not in result["message"], (
        f"a reservation request must not be answered with the delivery link: {result['message']!r}"
    )


def test_delivery_intent_matches_ordering_wording_only():
    from app.services.agent import _is_delivery_intent

    for text in [
        "quiero un domicilio", "¿hacen entregas a domicilio?", "me lo envían a la casa?",
        "para llevar por favor", "quiero recoger mi pedido", "pick up a las 7",
        "Hola, delivery?",
    ]:
        assert _is_delivery_intent(text) is True, text

    for text in [
        "quiero reservar una mesa para 4 el sábado", "cancelar mi reserva",
        "¿a qué hora abren?", "¿tienen menú vegetariano?", "hola", "",
    ]:
        assert _is_delivery_intent(text) is False, text


@pytest.mark.asyncio
async def test_system_prompt_carries_the_ordering_link_only_when_given():
    """Wording the keyword list misses still reaches the LLM, so the prompt
    tells it delivery is web-only and hands it the link."""
    from app.services.agent import build_system_prompt

    with_link = await build_system_prompt({}, None, web_order_url="https://x.test/pedir/abc")
    text_with = " ".join(b.get("text", "") for b in with_link)
    assert "https://x.test/pedir/abc" in text_with
    assert "NUNCA prometas tomar el pedido" in text_with

    without = await build_system_prompt({}, None)
    text_without = " ".join(b.get("text", "") for b in without)
    assert "PEDIDOS_A_DOMICILIO_Y_RECOGER" not in text_without
