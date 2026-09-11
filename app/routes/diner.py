"""
app/routes/diner.py
====================
Mesio-native diner chat surface (WhatsApp retirement wave).

A diner scans a QR at the table, which opens a Mesio-hosted chat page —
NOT WhatsApp. The bot is the product; the carta is presented INSIDE the
conversation via the `blocks` protocol (see app/services/blocks.py), not as
a separate page. This module is the HTTP surface for that page:

    POST /api/diner/session       — QR entry. Resolve table → org/location,
                                     mint a "web:<uuid4>" identity, return the
                                     bot's opening turn (greeting + carta).
    POST /api/diner/chat          — send one message, get {message, blocks}.
    GET  /api/diner/menu          — full carta for the "Ver carta completa" panel.
    POST /api/diner/waiter-call   — direct waiter ping (bill/cutlery/napkins/other),
                                     bypassing the LLM.

Security posture (CLAUDE.md "Diner traffic is UNAUTHENTICATED PUBLIC input"):
  - No admin/staff auth on any of these routes — anyone with a QR (or a
    guessed table_id) can open a session. Every field is treated as hostile.
  - Rate-limited via state_store.rate_limit_check (Redis, cross-worker) —
    never a module-level counter (CLAUDE.md Rule #7, cross-worker requirement).
  - Every diner message that reaches the LLM goes through agent.chat(), which
    internally wraps it via agent._wrap_user_message() before it ever touches
    the Anthropic API (see _build_enriched_user_message in agent.py). This
    module never talks to the LLM directly.
  - Tenant resolution: table_id → org_id happens BEFORE the tenant is known,
    so that lookup runs under bypass_tenant_scope() (mirrors
    app/routes/tables.py::public_menu_context and
    app/routes/dashboard.py::post_qr_claim). Everything after resolution runs
    inside tenant_scope(org_id), mirroring
    app/services/inbox_worker.py::_handle_meta_whatsapp (Rule 14).
  - Diner identities are opaque "web:<uuid4>" tokens — never a phone number.
    They flow into agent.chat()/carts/NPS/waiter_alerts exactly like a real
    WhatsApp number would (phone is an opaque identity string platform-wide).
    Phone/name are OPTIONAL and captured later, at payment time, via
    diner_sessions_repo.set_contact_info() — not implemented by this wave
    (payment/order-placement is a LATER wave per PM scope).

Table-context trick (no agent.py core changes needed):
  agent.detect_table_context() already has a well-tested path (Path 1) that
  opens a table_sessions row when the RAW message contains a `[t:<table_id>]`
  marker — this is exactly what a real QR-scan-without-phone-claim produces
  today. We reuse that path verbatim: the FIRST diner_chat message of a
  session carries the marker; once a table_sessions row exists,
  detect_table_context's Path 2 (active-session-by-phone lookup) picks it up
  automatically on every later turn, so the marker is only sent once (mirrors
  the real single-scan flow). This avoids touching agent.py's tool-execution
  internals, which CLAUDE.md gates hard ("Reglas del Bot — NO ROMPER").
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from app.services import blocks
from app.services import database as db
from app.services import state_store
from app.services.agent import chat as agent_chat
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope, tenant_scope
from app.repositories import diner_sessions_repo

log = get_logger(__name__)

router = APIRouter(prefix="/api/diner", tags=["diner"])

_WAITER_REASONS = ("bill", "cutlery", "napkins", "other")
_WAITER_MESSAGES = {
    "bill": "El cliente solicita la cuenta.",
    "cutlery": "El cliente solicita cubiertos.",
    "napkins": "El cliente solicita servilletas.",
    "other": "El cliente solicita asistencia.",
}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _features_dict(raw) -> dict:
    """Normalise a features value that may arrive as a JSON string or dict
    (asyncpg driver variability — same pattern used across the codebase,
    e.g. agent.py::_parse_features, scheduler.py::_features_dict)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


# ── Request models ──────────────────────────────────────────────────────────

class DinerSessionRequest(BaseModel):
    table_id: str = Field(..., min_length=1, max_length=100)


class DinerChatRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=4000)


class DinerWaiterCallRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    reason: str = Field(default="other", max_length=20)

    @field_validator("reason")
    @classmethod
    def _valid_reason(cls, v: str) -> str:
        if v not in _WAITER_REASONS:
            raise ValueError(f"reason must be one of {_WAITER_REASONS}")
        return v


# ── Shared helpers ───────────────────────────────────────────────────────────

async def _resolve_session_or_404(token: str) -> dict:
    """Pre-tenant lookup — token is the only thing the caller has.
    diner_sessions_repo.get_by_token() internally runs under
    bypass_tenant_scope (that lookup IS the tenant resolution)."""
    session = await diner_sessions_repo.get_by_token(token)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada")
    return session


def _dish_cards_for_category(dishes: list, availability: dict, currency: str) -> dict:
    """Project a raw menu category's dish list into a dish_cards block,
    filtering inactive dishes and attaching live stock (`available`)."""
    active_dishes = [d for d in (dishes or []) if isinstance(d, dict) and d.get("active", True)]
    dish_block = blocks.build_dish_cards_block(active_dishes, currency=currency)
    for d in dish_block["dishes"]:
        d["available"] = availability.get(d["name"], True)
    return dish_block


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/session")
async def create_diner_session(request: Request, body: DinerSessionRequest):
    """QR entry point. Resolves org/location/table from a scanned table_id,
    mints a new diner identity token, and returns the bot's opening turn.

    PM decision: the opening turn greets AND shows the carta immediately
    (greeting text + category_chips) — an empty "type something" prompt is
    explicitly rejected.
    """
    ip = _client_ip(request)
    allowed = await state_store.rate_limit_check(
        f"diner_session:{ip}", max_requests=20, window_seconds=60,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    table_id = body.table_id.strip()

    # Pre-tenant lookup — org unknown until the table resolves (mirrors
    # tables.py::public_menu_context / dashboard.py::post_qr_claim).
    with bypass_tenant_scope("diner_session_table_lookup"):
        table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")

    raw_org_id = table.get("org_id")
    if not raw_org_id:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    org_id = int(raw_org_id)
    location_id = table.get("branch_id")
    table_name = table.get("name") or table_id

    with tenant_scope(org_id):
        restaurant = await db.db_get_restaurant_by_id(location_id) if location_id else None
        if not restaurant or not restaurant.get("whatsapp_number"):
            raise HTTPException(status_code=404, detail="Restaurante no configurado para esta mesa")

        # Strip the "_b<timestamp>" sucursal suffix (see get_table_wa_number
        # in tables.py) so the bot_number matches what the inbox worker uses.
        bot_number = str(restaurant["whatsapp_number"]).split("_b")[0]
        restaurant_name = restaurant.get("name") or "nuestro restaurante"
        feats = _features_dict(restaurant.get("features"))
        currency = feats.get("currency", "COP")

        token = f"web:{uuid.uuid4()}"
        await diner_sessions_repo.create_session(
            token=token,
            org_id=org_id,
            bot_number=bot_number,
            location_id=int(location_id) if location_id else None,
            table_id=table_id,
            table_name=table_name,
            order_mode="dine_in",
        )

        # Deterministic — no LLM round-trip for a fixed opening template.
        menu = await db.db_get_menu(bot_number) or {}
        categories = [c for c, dishes in menu.items() if isinstance(dishes, list) and dishes]

    greeting = f"¡Hola! Bienvenido a {restaurant_name}, {table_name}. Esto es lo que tenemos hoy:"
    reply_blocks = []
    if categories:
        reply_blocks.append(blocks.build_category_chips_block(categories))

    log.info(
        "diner_session.opened",
        org_id=org_id,
        location_id=location_id,
        table_id=table_id,
    )

    return {
        "token": token,
        "org_id": org_id,
        "location_id": location_id,
        "table_id": table_id,
        "table_name": table_name,
        "restaurant_name": restaurant_name,
        "currency": currency,
        "message": greeting,
        "blocks": reply_blocks,
    }


@router.post("/chat")
async def diner_chat(request: Request, body: DinerChatRequest):
    """Send one diner message, get back {message, blocks}."""
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_chat_ip:{ip}", max_requests=60, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_chat:{token}", max_requests=20, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás enviando mensajes muy rápido. Espera un momento.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    location_id = session.get("location_id")
    user_message = body.message.strip()
    if not user_message:
        raise HTTPException(status_code=422, detail="Mensaje vacío")

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        # Category-chip shortcut ("cat:<Name>", see category_chips block built
        # by create_diner_session). Deterministic dict lookup against this
        # org's OWN menu — never concatenated into SQL or sent to the LLM, so
        # an arbitrary/hostile value here is inert: it either matches a real
        # category key or falls through to the normal chat path below.
        stripped = user_message
        if stripped.lower().startswith("cat:"):
            category = stripped[4:].strip()
            menu = await db.db_get_menu(bot_number) or {}
            dishes = menu.get(category)
            if isinstance(dishes, list) and dishes:
                feats = _features_dict((await db.db_get_restaurant_by_id(org_id) or {}).get("features"))
                currency = feats.get("currency", "COP")
                availability = await db.db_get_menu_availability(org_id)
                dish_block = _dish_cards_for_category(dishes, availability, currency)
                return {
                    "message": f"Esto es lo que tenemos en {category}:",
                    "blocks": [dish_block],
                }

        # Establish table context on the first message of a (re)opened
        # session — see module docstring "Table-context trick". Once a
        # table_sessions row exists, later turns pick it up automatically.
        raw_message = user_message
        table_id = session.get("table_id")
        if session.get("order_mode") == "dine_in" and table_id:
            active = await db.db_get_active_session(token, bot_number)
            if not active:
                raw_message = f"{user_message} [t:{table_id}]"

        result = await agent_chat(
            user_phone=token,
            user_message=raw_message,
            bot_number=bot_number,
            location_id=location_id,
        )

    if not result:
        # agent.chat() returns None for a few intentional-silence paths
        # (post-NPS guard, empty LLM reply already handled upstream).
        return {"message": "", "blocks": []}

    return {
        "message": result.get("message") or "",
        "blocks": result.get("blocks", []),
    }


@router.get("/menu")
async def diner_menu(token: str = Query(..., min_length=1, max_length=200)):
    """Full carta for the diner UI's 'Ver carta completa' panel."""
    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        restaurant = await db.db_get_restaurant_by_id(org_id)
        menu = await db.db_get_menu(bot_number) or {}
        availability = await db.db_get_menu_availability(org_id)

    feats = _features_dict((restaurant or {}).get("features"))
    currency = feats.get("currency", "COP")
    restaurant_name = (restaurant or {}).get("name") or "nuestro restaurante"

    categories = []
    for cat_name, dishes in menu.items():
        if not isinstance(dishes, list):
            continue
        dish_block = _dish_cards_for_category(dishes, availability, currency)
        if not dish_block["dishes"]:
            continue
        categories.append({"name": cat_name, "dishes": dish_block["dishes"]})

    return {
        "restaurant_name": restaurant_name,
        "currency": currency,
        "categories": categories,
    }


@router.post("/waiter-call")
async def diner_waiter_call(body: DinerWaiterCallRequest):
    """Direct waiter ping — bypasses the LLM entirely. Reuses the EXISTING
    waiter_alerts mechanism (tables_repo.db_create_waiter_alert) — no
    parallel table/queue."""
    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_waiter_call:{token}", max_requests=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="Ya avisamos al mesero. Espera un momento.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    reason = body.reason

    message_text = _WAITER_MESSAGES.get(reason, _WAITER_MESSAGES["other"])
    table_name = session.get("table_name") or ""
    if table_name:
        message_text = f"{message_text} ({table_name})"

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        await db.db_create_waiter_alert(
            phone=token,
            bot_number=bot_number,
            alert_type=reason,
            message=message_text,
            table_id=session.get("table_id") or "",
            table_name=table_name,
        )

    log.info("diner.waiter_call", org_id=org_id, reason=reason)

    return {
        "message": "Listo, ya avisamos al mesero.",
        "blocks": [blocks.build_waiter_ack_block(reason, message_text)],
    }
