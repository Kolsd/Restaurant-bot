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
from app.services import orders
from app.services import state_store
from app.services.agent import chat as agent_chat, _generate_join_code, _JOIN_CODE_RE
from app.services.logging import get_logger
from app.services.money import quantize_money, to_decimal
from app.services.table_order_commit import deduct_inventory_or_cancel, save_table_order_round
from app.services.tenant_context import bypass_tenant_scope, tenant_scope
from app.repositories import diner_sessions_repo, tables_repo

log = get_logger(__name__)

router = APIRouter(prefix="/api/diner", tags=["diner"])

# Join-code brute force: throttle both per-identity (this diner's own token)
# and per-table (many tokens hammering the same 4-digit code from repeated
# /session calls) — see module docstring "Capa 2" in agent.py for the
# original WhatsApp-flow protections this mirrors.
_JOIN_WRONG_MAX_PER_TOKEN = 3
_JOIN_WRONG_WINDOW_PER_TOKEN = 600      # 10 min
_JOIN_WRONG_MAX_PER_TABLE = 10
_JOIN_WRONG_WINDOW_PER_TABLE = 600

# Order-send idempotency cache TTL — long enough to cover a slow retry /
# double-tap, short enough that a stale key can't shadow a genuinely new
# order much later in the same session.
_ORDER_SEND_CACHE_TTL = 300

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


class DinerCartAddRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    qty: int = Field(..., ge=1, le=50)
    note: str | None = Field(default=None, max_length=200)


class DinerCartUpdateRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    line_id: str = Field(..., min_length=1, max_length=64)
    qty: int | None = Field(default=None, ge=0, le=50)
    note: str | None = Field(default=None, max_length=200)


class DinerCartRemoveRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    line_id: str = Field(..., min_length=1, max_length=64)


class DinerJoinRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    code: str = Field(..., min_length=1, max_length=10)


class DinerOrderSendRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    idempotency_key: str = Field(..., min_length=1, max_length=100)


# ── Shared helpers ───────────────────────────────────────────────────────────

async def _resolve_session_or_404(token: str) -> dict:
    """Pre-tenant lookup — token is the only thing the caller has.
    diner_sessions_repo.get_by_token() internally runs under
    bypass_tenant_scope (that lookup IS the tenant resolution)."""
    session = await diner_sessions_repo.get_by_token(token)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada")
    return session


async def _resolve_diner_restaurant(org_id: int, location_id: int | None) -> dict | None:
    """Safe replacement for `db.db_get_restaurant_by_id(location_id)`.

    P0 (found 2026-09, separate fix wave in progress on db_get_restaurant_by_id
    itself): that function's SQL is `WHERE r.id = $1 OR l.org_id = $1 ORDER BY
    (l.org_id = $1) DESC` — it accepts EITHER an org id OR a location id, and
    org ids/location ids are independent sequences over the same integer
    range, so passing a LOCATION id can resolve to a COMPLETELY DIFFERENT
    org's restaurant whenever some other org happens to share that id.
    Passing an ORG id is safe (the org-id branch always wins the ORDER BY).

    This helper never passes a location id into that function. It fetches
    the org's own row by org_id (safe) for `features`/fallback name+number,
    and separately fetches the location row by PK — explicitly checking
    `location.org_id == org_id` before trusting anything from it, so a
    location id colliding with an unrelated org's id can never leak that
    org's name/whatsapp_number into this session.
    """
    org_restaurant = await db.db_get_restaurant_by_id(org_id)
    if not org_restaurant:
        return None

    location = await db.db_get_location_by_id(location_id) if location_id else None
    if location and int(location.get("org_id") or -1) != org_id:
        log.warning(
            "diner.location_org_mismatch",
            org_id=org_id, location_id=location_id, location_org_id=location.get("org_id"),
        )
        location = None

    # Mirror the `restaurants` VIEW's own precedence exactly (see migration
    # 0037's `CREATE VIEW restaurants`): org name wins over location name
    # (COALESCE(o.name, l.name)), but location's own WhatsApp number wins
    # over the org's (COALESCE(l.whatsapp_number, o.whatsapp_number)) — the
    # two fields intentionally fall back in OPPOSITE directions.
    merged = dict(org_restaurant)
    merged["name"] = org_restaurant.get("name") or (location.get("name") if location else None)
    merged["whatsapp_number"] = (
        (location.get("whatsapp_number") if location else None) or org_restaurant.get("whatsapp_number")
    )
    merged["location_id"] = location_id
    return merged


def _dish_cards_for_category(dishes: list, availability: dict, currency: str) -> dict:
    """Project a raw menu category's dish list into a dish_cards block,
    filtering inactive dishes and attaching live stock (`available`)."""
    active_dishes = [d for d in (dishes or []) if isinstance(d, dict) and d.get("active", True)]
    dish_block = blocks.build_dish_cards_block(active_dishes, currency=currency)
    for d in dish_block["dishes"]:
        d["available"] = availability.get(d["name"], True)
    return dish_block


async def _currency_for_org(org_id: int) -> str:
    """Caller must already be inside tenant_scope(org_id)."""
    restaurant = await db.db_get_restaurant_by_id(org_id)
    feats = _features_dict((restaurant or {}).get("features"))
    return feats.get("currency", "COP")


def _cart_blocks_response(cart: dict, currency: str, message: str) -> dict:
    """Every cart tap-endpoint returns the SAME shape: the updated cart as a
    cart_summary block (even when empty — allow_empty=True — so the UI can
    render the empty state / clear the cart chip) plus a short confirmation."""
    block = blocks.build_cart_summary_block(cart, currency=currency, allow_empty=True)
    return {"blocks": [block], "message": message}


def _cart_error_to_http(error: str) -> HTTPException:
    """Map an orders.py cart-mutation error string to the right HTTP status.
    Never a 500 — NO-ROMPER #5 (cart lock contention must surface as a
    friendly message, not a crash)."""
    if error == "not_found":
        return HTTPException(status_code=404, detail="No encontramos ese producto en tu pedido")
    if "siendo procesado" in error:
        return HTTPException(status_code=409, detail=error)
    return HTTPException(status_code=422, detail=error or "No pudimos actualizar tu pedido")


async def _opening_turn(bot_number: str, restaurant_name: str, table_name: str) -> dict:
    """Deterministic opening turn (greeting + category chips) — shared by a
    fresh scan on a free table (create_diner_session) and a participant who
    just supplied the right join code (diner_join). No LLM round-trip for a
    fixed template. Caller must already be inside tenant_scope(org_id)."""
    menu = await db.db_get_menu(bot_number) or {}
    categories = [c for c, dishes in menu.items() if isinstance(dishes, list) and dishes]
    reply_blocks = []
    if categories:
        reply_blocks.append(blocks.build_category_chips_block(categories))
    return {
        "message": f"¡Hola! Bienvenido a {restaurant_name}, {table_name}. Esto es lo que tenemos hoy:",
        "blocks": reply_blocks,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

@router.post("/session")
async def create_diner_session(request: Request, body: DinerSessionRequest):
    """QR entry point. Resolves org/location/table from a scanned table_id,
    mints a new diner identity token, and opens (or asks to join) the table
    session — see module docstring "Table session at scan time".

    Free table (no other active session): opens the table_sessions row for
    this diner right here, mints a 4-digit join_code so a second diner can
    join, marks it verified (PM decision: web orders go straight to the
    kitchen, no waiter-approval hold), and returns the bot's opening turn
    (greeting + category_chips) plus the join_code for the UI to display.

    Occupied table (another phone already has an active session): does NOT
    open a session and does NOT show the greeting/carta — returns
    requires_join_code=True so the UI asks for the code and calls
    POST /api/diner/join. The diner_sessions row (token) is still created so
    that join call has something to resolve.
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
        restaurant = await _resolve_diner_restaurant(org_id, location_id)
        if not restaurant or not restaurant.get("whatsapp_number"):
            raise HTTPException(status_code=404, detail="Restaurante no configurado para esta mesa")

        # Strip the "_b<timestamp>" sucursal suffix (see get_table_wa_number
        # in tables.py) so the bot_number matches what the inbox worker uses.
        bot_number = str(restaurant["whatsapp_number"]).split("_b")[0]
        restaurant_name = restaurant.get("name") or "nuestro restaurante"
        feats = _features_dict(restaurant.get("features"))
        currency = feats.get("currency", "COP")
        location_id_int = int(location_id) if location_id else None

        token = f"web:{uuid.uuid4()}"
        await diner_sessions_repo.create_session(
            token=token,
            org_id=org_id,
            bot_number=bot_number,
            location_id=location_id_int,
            table_id=table_id,
            table_name=table_name,
            order_mode="dine_in",
        )

        # Capa 2 (mirrors agent.detect_table_context): is this table already
        # held by someone else? `token` is brand new, so `phone<>token` is
        # trivially true for any real existing session on the table.
        other_session = await tables_repo.db_get_active_session_on_table_by_other_phone(table_id, token)
        if other_session:
            log.info(
                "diner_session.join_code_required",
                org_id=org_id, location_id=location_id, table_id=table_id,
            )
            return {
                "token": token,
                "org_id": org_id,
                "location_id": location_id,
                "table_id": table_id,
                "table_name": table_name,
                "restaurant_name": restaurant_name,
                "currency": currency,
                "requires_join_code": True,
                "message": "",
                "blocks": [],
            }

        # Free table — open it, mint the join_code, and mark it verified:
        # web diners scanned the real physical QR, which is at least as
        # trustworthy as the WhatsApp geo-claim flow — no waiter hold.
        new_session = await tables_repo.db_create_table_session(
            token, bot_number, table_id, table_name,
            org_id=org_id, location_id=location_id_int,
        )
        join_code = _generate_join_code()
        await tables_repo.db_set_session_join_code(new_session["id"], join_code)
        await tables_repo.db_mark_session_verified(new_session["id"])

        turn = await _opening_turn(bot_number, restaurant_name, table_name)

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
        "requires_join_code": False,
        "join_code": join_code,
        "message": turn["message"],
        "blocks": turn["blocks"],
    }


@router.post("/join")
async def diner_join(request: Request, body: DinerJoinRequest):
    """Second (or later) diner supplies the join_code shown on the first
    diner's screen. Validates it against the table's active session
    (tables_repo.db_link_participant_session), opens a NEW table_sessions
    row for this diner (own phone=token, so their own orders are attributed
    to them), marks it verified, and returns the normal opening turn.

    Throttled per-identity AND per-table (module constants) — a 4-digit
    code is brute-forceable in ~5000 guesses on average with no limit.
    """
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_join_ip:{ip}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_join:{token}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás intentando muy rápido. Espera un momento.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    location_id = session.get("location_id")
    table_id = session.get("table_id")
    table_name = session.get("table_name") or table_id
    if not table_id:
        raise HTTPException(status_code=422, detail="Esta sesión no está asociada a una mesa")

    match = _JOIN_CODE_RE.match(body.code)
    if not match:
        raise HTTPException(status_code=422, detail="Ingresa el código de 4 dígitos que te dio quien abrió la mesa.")
    code = match.group(1)

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        # Idempotent re-join: already linked (e.g. a retried request after a
        # success the client didn't see) — just return the greeting again,
        # never a second table_sessions row.
        already = await db.db_get_active_session(token, bot_number)
        if already:
            restaurant = await db.db_get_restaurant_by_id(org_id)
            restaurant_name = (restaurant or {}).get("name") or "nuestro restaurante"
            currency = _features_dict((restaurant or {}).get("features")).get("currency", "COP")
            turn = await _opening_turn(bot_number, restaurant_name, table_name)
            return {**turn, "restaurant_name": restaurant_name, "currency": currency, "table_name": table_name}

        new_session = await tables_repo.db_link_participant_session(
            phone=token,
            bot_number=bot_number,
            table_id=table_id,
            table_name=table_name,
            join_code=code,
            org_id=org_id,
            location_id=location_id,
        )

        if new_session is None:
            # Wrong code — throttle further attempts, both by this diner and
            # by the table as a whole (many tokens hammering one table).
            table_ok = await state_store.rate_limit_check(
                f"diner_join_wrong_table:{table_id}",
                max_requests=_JOIN_WRONG_MAX_PER_TABLE, window_seconds=_JOIN_WRONG_WINDOW_PER_TABLE,
            )
            token_ok = await state_store.rate_limit_check(
                f"diner_join_wrong_token:{token}",
                max_requests=_JOIN_WRONG_MAX_PER_TOKEN, window_seconds=_JOIN_WRONG_WINDOW_PER_TOKEN,
            )
            log.warning("diner_join.wrong_code", org_id=org_id, table_id=table_id)
            if not table_ok or not token_ok:
                raise HTTPException(
                    status_code=422,
                    detail="Demasiados intentos fallidos. Pídele el código a quien abrió la mesa.",
                )
            raise HTTPException(status_code=422, detail="Código incorrecto. Intenta de nuevo.")

        await tables_repo.db_mark_session_verified(new_session["id"])

        restaurant = await db.db_get_restaurant_by_id(org_id)
        restaurant_name = (restaurant or {}).get("name") or "nuestro restaurante"
        currency = _features_dict((restaurant or {}).get("features")).get("currency", "COP")
        turn = await _opening_turn(bot_number, restaurant_name, table_name)

    log.info("diner_join.success", org_id=org_id, table_id=table_id, session_id=new_session.get("id"))

    return {**turn, "restaurant_name": restaurant_name, "currency": currency, "table_name": table_name}


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
            location_id=session.get("location_id"),
        )

    log.info("diner.waiter_call", org_id=org_id, reason=reason)

    return {
        "message": "Listo, ya avisamos al mesero.",
        "blocks": [blocks.build_waiter_ack_block(reason, message_text)],
    }


# ── Cart endpoints (direct tap mutations — no LLM round-trip) ───────────────
#
# A tap on "+"/"-", a note edit, or a remove button has nothing for the LLM
# to interpret: it's a deterministic (sku|name, qty, note) tuple. Routing it
# through agent.chat() would cost tokens against the restaurant's plan caps,
# add latency, and — with no ANTHROPIC_API_KEY configured — fail outright
# (Rule 8 fallback). These four endpoints never call agent_chat / the
# Anthropic client; the LLM path (typing "quiero una bandeja sin
# chicharrón") still goes through agent.chat() → the place_order tool →
# orders.add_to_cart(), which shares the same line_id/note-aware cart model.

@router.post("/cart/add")
async def diner_cart_add(request: Request, body: DinerCartAddRequest):
    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_cart_add:{token}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás agregando platos muy rápido. Espera un momento.")

    sku = (body.sku or "").strip() or None
    name = (body.name or "").strip() or None
    if not sku and not name:
        raise HTTPException(status_code=422, detail="Falta indicar el plato a agregar")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        dish = await orders.resolve_dish_for_cart(bot_number, org_id, sku=sku, name=name)
        if dish is None:
            raise HTTPException(
                status_code=404,
                detail="No encontramos ese plato o no está disponible en este momento",
            )

        result = await orders.add_cart_line(token, bot_number, dish, body.qty, body.note)
        if not result.get("success"):
            raise _cart_error_to_http(result.get("error", ""))

        currency = await _currency_for_org(org_id)
        cart = result["cart"]

    return _cart_blocks_response(cart, currency, f"Agregado: {dish['name']}")


@router.post("/cart/update")
async def diner_cart_update(request: Request, body: DinerCartUpdateRequest):
    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_cart_update:{token}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás actualizando tu pedido muy rápido. Espera un momento.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        result = await orders.update_cart_line(token, bot_number, body.line_id, qty=body.qty, note=body.note)
        if not result.get("success"):
            raise _cart_error_to_http(result.get("error", ""))

        currency = await _currency_for_org(org_id)
        cart = result["cart"]

    message = "Listo, quitamos ese producto de tu pedido." if body.qty == 0 else "Listo, actualizamos tu pedido."
    return _cart_blocks_response(cart, currency, message)


@router.post("/cart/remove")
async def diner_cart_remove(request: Request, body: DinerCartRemoveRequest):
    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_cart_remove:{token}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Espera un momento antes de seguir editando tu pedido.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        result = await orders.remove_cart_line(token, bot_number, body.line_id)
        if not result.get("success"):
            raise _cart_error_to_http(result.get("error", ""))

        currency = await _currency_for_org(org_id)
        cart = result["cart"]

    return _cart_blocks_response(cart, currency, "Listo, quitamos ese producto de tu pedido.")


@router.get("/cart")
async def diner_cart_get(token: str = Query(..., min_length=1, max_length=200)):
    if not await state_store.rate_limit_check(f"diner_cart_get:{token}", max_requests=60, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        cart = await orders.get_cart_with_line_ids(token, bot_number)
        currency = await _currency_for_org(org_id)

    return _cart_blocks_response(cart, currency, "")


# ── Send order (deterministic — reuses the shared table-order-commit core) ──
#
# The confirm-and-send tap is the diner-web equivalent of the WhatsApp bot's
# place_order tool (agent_salon.execute_salon_action, action="order"). Both
# paths now call app.services.table_order_commit.save_table_order_round /
# deduct_inventory_or_cancel — same station routing, same base_order_id/
# sub_number numbering, same typed InsufficientStockError handling
# (NO-ROMPER #13) — so the kitchen/bar/mesero/caja screens see a web order
# exactly like a bot order.

@router.post("/order/send")
async def diner_order_send(request: Request, body: DinerOrderSendRequest):
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_order_send_ip:{ip}", max_requests=30, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_order_send:{token}", max_requests=10, window_seconds=60):
        raise HTTPException(status_code=429, detail="Estás enviando pedidos muy rápido. Espera un momento.")

    idem_key = body.idempotency_key.strip()
    cache_key = f"{token}:{idem_key}"

    # Fast path: a retry/double-tap with the SAME idempotency_key returns the
    # exact result of the original send — never a second table_order round.
    cached = await state_store.order_send_result_get(cache_key)
    if cached:
        return cached

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    location_id = session.get("location_id")
    table_id = session.get("table_id")
    table_name = session.get("table_name") or table_id
    if session.get("order_mode") != "dine_in" or not table_id:
        raise HTTPException(status_code=422, detail="Esta sesión no está asociada a una mesa")

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        # Must have actually joined the table (scanned a free table, or
        # supplied the right join_code) — browsing/adding to cart before
        # joining is allowed, sending the order to the kitchen is not.
        active = await db.db_get_active_session(token, bot_number)
        if not active:
            raise HTTPException(
                status_code=422,
                detail="Únete a la mesa con el código antes de enviar tu pedido.",
            )

        try:
            async with orders._cart_lock(token, bot_number):
                # Re-check the cache inside the lock — a concurrent duplicate
                # request may have just finished and populated it.
                cached = await state_store.order_send_result_get(cache_key)
                if cached:
                    return cached

                cart = await db.db_get_cart(token, bot_number)
                cart_items = cart.get("items") or []
                if not cart_items:
                    raise HTTPException(status_code=422, detail="Tu pedido está vacío")

                cart_total = await orders.get_cart_total(token, bot_number)
                currency = await _currency_for_org(org_id)

                restaurant = await _resolve_diner_restaurant(org_id, location_id)
                features = _features_dict((restaurant or {}).get("features"))

                table_context = {
                    "id": table_id,
                    "name": table_name,
                    "org_id": org_id,
                    "branch_id": location_id,
                }
                base_order_id = await tables_repo.db_get_base_order_id(table_id)
                is_new_group = base_order_id is None

                commit = await save_table_order_round(
                    is_new_group=is_new_group,
                    existing_base_order_id=base_order_id,
                    table_context=table_context,
                    restaurant_obj=restaurant,
                    phone=token,
                    cart_items=cart_items,
                    cart_total=cart_total,
                    extra_notes="",
                    channel="web_chat",
                    pending_table_validation=False,
                    features=features,
                )
                if not commit["success"]:
                    log.error("diner_order_send.commit_failed", org_id=org_id, table_id=table_id)
                    raise HTTPException(
                        status_code=500,
                        detail="Hubo un problema al registrar tu pedido. Por favor pide ayuda al mesero.",
                    )

                inv = await deduct_inventory_or_cancel(bot_number, cart_items, commit["order_id"])
                if not inv["success"]:
                    # Deliberately does NOT clear the cart here (unlike the
                    # WhatsApp path, which wipes it — see agent_salon.py):
                    # the diner has a real cart-editing UI, so leaving it
                    # intact lets them remove just the sold-out item and
                    # resend the rest, instead of re-typing everything.
                    # The already-cancelled order row is invisible to the
                    # kitchen/table views (both filter out status=cancelled).
                    raise HTTPException(
                        status_code=422,
                        detail=f"{inv['message']} ¿Quieres ajustar tu pedido y volver a intentar?",
                    )

                # NOT orders.clear_cart() — that acquires the SAME cart lock
                # we're already holding (orders._cart_lock is not
                # re-entrant), which would silently no-op on contention and
                # leave the cart un-cleared (found while testing: a second
                # send with a fresh idempotency_key would then re-order the
                # same items instead of hitting "carrito vacío"). We already
                # hold the lock for this whole critical section, so clear
                # the cart directly.
                await db.db_clear_cart(token, bot_number)
                await tables_repo.db_session_mark_order(token, bot_number)
        except RuntimeError as exc:
            if "cart_lock_contention" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail="Tu pedido está siendo procesado, por favor espera un momento.",
                )
            raise

    response = {
        "order_id": commit["order_id"],
        "base_order_id": commit["base_order_id"],
        "sub_number": commit["sub_number"],
        "total": float(quantize_money(to_decimal(cart_total), currency)),  # JSON boundary
        "items": [
            {
                "name": i.get("name", ""),
                "qty": int(i.get("quantity") or 1),
                "notes": (i.get("note") or "").strip(),
            }
            for i in cart_items
        ],
        "message": "Listo, tu pedido ya va para la cocina.",
    }
    await state_store.order_send_result_set(cache_key, response, ttl_seconds=_ORDER_SEND_CACHE_TTL)

    log.info(
        "diner_order_send.success",
        org_id=org_id, table_id=table_id, order_id=commit["order_id"],
        base_order_id=commit["base_order_id"], sub_number=commit["sub_number"],
    )
    return response


# ── Table view — "Tú" vs "Otro comensal" (Gap 3) ────────────────────────────

@router.get("/table")
async def diner_table_view(token: str = Query(..., min_length=1, max_length=200)):
    """Every order at the diner's table, grouped by who ordered it. Never
    exposes another diner's `web:` token or phone — just "Otro comensal".
    A diner who hasn't joined a table session (not a participant) gets a 404,
    same as an unknown token."""
    if not await state_store.rate_limit_check(f"diner_table_view:{token}", max_requests=60, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    bot_number = session["bot_number"]
    location_id = session.get("location_id")
    table_id = session.get("table_id")

    with tenant_scope(org_id):
        active = await db.db_get_active_session(token, bot_number)
        if not active or not table_id:
            raise HTTPException(status_code=404, detail="No estás en una mesa")

        currency = await _currency_for_org(org_id)
        base_order_id = await tables_repo.db_get_base_order_id(table_id)
        rows = (
            await tables_repo.db_get_table_orders_by_base_id(base_order_id, branch_id=location_id)
            if base_order_id else []
        )

    orders_out = []
    for row in rows:
        if row.get("status") in ("cancelado", "cancelled"):
            continue
        items = row.get("items") or []
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except (ValueError, TypeError):
                items = []
        row_phone = row.get("phone") or ""
        is_mine = row_phone == token
        created_at = row.get("created_at")
        orders_out.append({
            "order_id": row.get("id"),
            "sub_number": row.get("sub_number") or 1,
            "status": row.get("status") or "recibido",
            "station": row.get("station") or "all",
            "items": [
                {
                    "name": it.get("name", "") if isinstance(it, dict) else "",
                    "qty": int((it.get("quantity") or 1)) if isinstance(it, dict) else 1,
                    "notes": ((it.get("notes") or it.get("note") or "").strip()) if isinstance(it, dict) else "",
                }
                for it in items
            ],
            "total": float(quantize_money(to_decimal(row.get("total") or 0), currency)),  # JSON boundary
            "diner_label": "Tú" if is_mine else "Otro comensal",
            "mine": is_mine,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
        })

    orders_out.sort(key=lambda o: (o["created_at"] or "", o["sub_number"]))

    return {
        "table_name": session.get("table_name") or table_id,
        "currency": currency,
        "orders": orders_out,
    }
