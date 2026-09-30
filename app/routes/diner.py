"""
app/routes/diner.py
====================
Mesio-native diner chat surface (WhatsApp retirement wave).

A diner scans a QR at the table, which opens a Mesio-hosted chat page —
NOT WhatsApp. The bot is the product; the menu is presented INSIDE the
conversation via the `blocks` protocol (see app/services/blocks.py), not as
a separate page. This module is the HTTP surface for that page:

    POST /api/diner/session       — QR entry. Resolve table → org/location,
                                     mint a "web:<uuid4>" identity, return the
                                     bot's opening turn (greeting + menu).
    POST /api/diner/chat          — send one message, get {message, blocks}.
    GET  /api/diner/menu          — full menu for the "Ver carta completa" panel.
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
    so that lookup runs under bypass_tenant_scope(). Everything after
    resolution runs inside tenant_scope(org_id) (Rule 14).
  - Diner identities are opaque "web:<uuid4>" tokens — never a phone number.
    They flow into agent.chat()/carts/NPS/waiter_alerts as the `phone` key
    (an opaque identity string platform-wide).
    Phone/name are OPTIONAL and captured later, at payment time, via
    diner_sessions_repo.set_contact_info() — not implemented by this wave
    (payment/order-placement is a LATER wave per PM scope).

Table context: agent.detect_table_context() reads the diner's active
  table_sessions row. When a dine-in diner writes and that row does not exist
  (first message, or the previous sitting was closed), diner_chat opens it
  from the table bound to the diner session — never from anything the diner
  typed. Until 2026-09-25 this went through a `[t:<table_id>]` marker
  appended to the message, which also let a diner TYPE another table's
  marker and open a session there.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from app.services import blocks
from app.services import database as db
from app.services import delivery as delivery_service
from app.services import orders
from app.services import plan_access, plans
from app.services import realtime
from app.services import sede_menu
from app.services import state_store
from app.services.naming import restaurant_display_name
from app.services import turnstile
from app.services.agent import chat as agent_chat, _generate_join_code, _JOIN_CODE_RE
from app.services.logging import get_logger
from app.services.money import ZERO, format_money_es, money_mul, money_sum, quantize_money, to_decimal
from app.services.table_order_commit import deduct_inventory_or_cancel, save_table_order_round
from app.services.tenant_context import bypass_tenant_scope, tenant_scope
from app.repositories import delivery_repo, diner_sessions_repo, tables_repo

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


def _parse_items_list(raw) -> list:
    """Normalise a JSONB `items` column that may arrive as a JSON string
    (asyncpg driver variability) or already a list."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = []
    return raw if isinstance(raw, list) else []


# ── Request models ──────────────────────────────────────────────────────────

_ORDER_MODES = ("dine_in", "delivery", "pickup")


class DinerSessionRequest(BaseModel):
    """QR entry (order_mode="dine_in", the original/default shape — table_id
    required) OR delivery/pickup entry (docs/claude/delivery-web.md chunk 2):
    the frontend has already called GET /api/diner/org/{slug} and
    POST /api/diner/order-mode/resolve, so this call carries the already-
    resolved slug + location_id, never raw GPS — this endpoint does NOT
    re-run the sede-assignment ladder, it only opens the session."""

    order_mode: str = Field(default="dine_in", max_length=20)
    table_id: str | None = Field(default=None, min_length=1, max_length=100)
    slug: str | None = Field(default=None, min_length=1, max_length=100)
    location_id: int | None = Field(default=None, gt=0)
    turnstile_token: str | None = Field(default=None, max_length=2000)

    @field_validator("order_mode")
    @classmethod
    def _valid_order_mode(cls, v: str) -> str:
        if v not in _ORDER_MODES:
            raise ValueError(f"order_mode must be one of {_ORDER_MODES}")
        return v

    @model_validator(mode="after")
    def _cross_field_requirements(self) -> "DinerSessionRequest":
        if self.order_mode == "dine_in":
            if not self.table_id:
                raise ValueError("table_id is required for order_mode=dine_in")
        else:
            if self.table_id:
                raise ValueError(f"table_id must not be set for order_mode={self.order_mode}")
            if not self.slug or not self.location_id:
                raise ValueError(f"slug and location_id are required for order_mode={self.order_mode}")
        return self


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


_CHECKOUT_SCOPES = ("mine", "table")
_CHECKOUT_METHODS = ("card", "cash")
_CHECKOUT_METHOD_LABELS = {"card": "tarjeta", "cash": "efectivo"}
_CHECKOUT_METHOD_DISPLAY = {"card": "Tarjeta", "cash": "Efectivo"}


class DinerCheckoutRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=200)
    scope: str = Field(..., max_length=10)
    method: str = Field(..., max_length=10)
    tip_amount: float = Field(default=0.0, ge=0)
    customer_name: str | None = Field(default=None, max_length=100)
    customer_phone: str | None = Field(default=None, max_length=30)

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        if v not in _CHECKOUT_SCOPES:
            raise ValueError(f"scope must be one of {_CHECKOUT_SCOPES}")
        return v

    @field_validator("method")
    @classmethod
    def _valid_method(cls, v: str) -> str:
        if v not in _CHECKOUT_METHODS:
            raise ValueError(f"method must be one of {_CHECKOUT_METHODS}")
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


async def _resolve_diner_restaurant(org_id: int, location_id: int | None) -> dict | None:
    """Safe replacement for the now-deleted, ambiguous db_get_restaurant_by_id lookup (took a location_id).

    P0 (found 2026-09, fixed across the whole app in the same wave): that
    function's SQL was `WHERE r.id = $1 OR l.org_id = $1 ORDER BY
    (l.org_id = $1) DESC` — it accepted EITHER an org id OR a location id, and
    org ids/location ids are independent sequences over the same integer
    range, so passing a LOCATION id could resolve to a COMPLETELY DIFFERENT
    org's restaurant whenever some other org happens to share that id.
    Passing an ORG id is safe (the org-id branch always wins the ORDER BY).

    This helper never passes a location id into that function. It fetches
    the org's own row by org_id (safe) for `features`/fallback name+number,
    and separately fetches the location row by PK — explicitly checking
    `location.org_id == org_id` before trusting anything from it, so a
    location id colliding with an unrelated org's id can never leak that
    org's name into this session.
    """
    org_restaurant = await db.db_get_restaurant_by_org_id(org_id)
    if not org_restaurant:
        return None

    location = await db.db_get_location_by_id(location_id) if location_id else None
    if location and int(location.get("org_id") or -1) != org_id:
        log.warning(
            "diner.location_org_mismatch",
            org_id=org_id, location_id=location_id, location_org_id=location.get("org_id"),
        )
        location = None

    # Mirror the `restaurants` VIEW: org name wins over location name
    # (COALESCE(o.name, l.name)).
    merged = dict(org_restaurant)
    merged["name"] = org_restaurant.get("name") or (location.get("name") if location else None)
    merged["location_id"] = location_id

    # What the diner is told they are ordering from. The `restaurants` view
    # computes this as `display_name` (migration 0092), but this helper
    # merges an org row with a location row by hand rather than reading the
    # view, so the same rule is applied here — see app/services/naming.py.
    sedes = await db.db_get_org_locations(org_id)
    merged["display_name"] = restaurant_display_name(
        org_restaurant.get("name"),
        location.get("name") if location else None,
        len(sedes or []),
    )
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
    restaurant = await db.db_get_restaurant_by_org_id(org_id)
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


async def _opening_turn(
    org_id: int, location_id: int | None, restaurant_name: str, table_name: str | None = None,
) -> dict:
    """Deterministic opening turn (greeting + category chips) — shared by a
    fresh scan on a free table (create_diner_session), a participant who
    just supplied the right join code (diner_join), and a delivery/pickup
    session (no table_name — docs/claude/delivery-web.md chunk 2). No LLM
    round-trip for a fixed template. Caller must already be inside
    tenant_scope(org_id). The chips are the categories of THIS sede's carta
    (migration 0093), which may hold categories of its own."""
    menu = await sede_menu.get_sede_menu(org_id, location_id)
    categories = [c for c, dishes in menu.items() if isinstance(dishes, list) and dishes]
    reply_blocks = []
    if categories:
        reply_blocks.append(blocks.build_category_chips_block(categories))
    greeting = (
        f"¡Hola! Bienvenido a {restaurant_name}, {table_name}. Esto es lo que tenemos hoy:"
        if table_name else
        f"¡Hola! Bienvenido a {restaurant_name}. Esto es lo que tenemos hoy:"
    )
    return {
        "message": greeting,
        "blocks": reply_blocks,
    }


# ── Routes ───────────────────────────────────────────────────────────────────

async def _create_delivery_pickup_session(body: DinerSessionRequest, ip: str) -> dict:
    """order_mode=delivery|pickup entry (docs/claude/delivery-web.md chunk 2).

    Unlike the dine-in path, the sede is already resolved by the time this
    is called — the frontend calls GET /api/diner/org/{slug} and
    POST /api/diner/order-mode/resolve first, so `body.slug` +
    `body.location_id` are trusted-shape but still HOSTILE input (public,
    unauthenticated) that must be validated against the real org/location
    relationship, never assumed. table_id stays NULL; location_id carries
    the resolved sede (docs/claude/delivery-web.md, item C.3).
    """
    # Cloudflare Turnstile — delivery/pickup only, never the dine-in QR path
    # (item D). No-op success when TURNSTILE_SECRET is unset (local/test).
    if turnstile.is_configured():
        if not body.turnstile_token or not await turnstile.verify(body.turnstile_token, remote_ip=ip):
            raise HTTPException(status_code=422, detail="Verificación de seguridad fallida. Intenta de nuevo.")

    # Pre-tenant lookup — org unknown until the slug resolves (mirrors the
    # dine-in path's table lookup below; db_get_org_by_slug enters
    # bypass_tenant_scope internally, so this is never nested in a second one).
    org = await delivery_repo.db_get_org_by_slug(body.slug.strip())
    if not org:
        raise HTTPException(status_code=404, detail="Restaurante no encontrado")
    org_id = int(org["id"])
    location_id = int(body.location_id)

    with tenant_scope(org_id):
        # org_id and location_id are DISTINCT integers — never trust the
        # caller's pairing without checking the location actually belongs to
        # this org (same P0 shape as the deleted db_get_restaurant_by_id).
        location = await db.db_get_location_by_id(location_id)
        if not location or int(location.get("org_id") or -1) != org_id:
            raise HTTPException(status_code=404, detail="Sede no encontrada")

        # db_get_location_by_id() does not select delivery_config (chunk 1
        # added that column to `locations` but did not touch restaurant_repo's
        # generic getters) — fetch it through delivery_repo, the single
        # source of truth for that JSONB, and hand it to get_delivery_config()
        # exactly as docs/claude/delivery-web.md requires ("call the config
        # resolver, never re-read the JSONB yourself").
        raw_delivery_config = await delivery_repo.db_get_location_delivery_config(org_id, location_id)
        cfg = delivery_service.get_delivery_config(org, {**location, "delivery_config": raw_delivery_config})
        if body.order_mode == "delivery" and not cfg["delivery_enabled"]:
            raise HTTPException(status_code=422, detail="Esta sede no tiene domicilios activos")
        if body.order_mode == "pickup" and not cfg["pickup_enabled"]:
            raise HTTPException(status_code=422, detail="Esta sede no tiene recogida en tienda activa")

        restaurant = await _resolve_diner_restaurant(org_id, location_id)
        if not restaurant:
            raise HTTPException(status_code=404, detail="Restaurante no configurado para esta sede")
        # display_name says WHICH sede when the org has several (0092).
        restaurant_name = (
            restaurant.get("display_name") or restaurant.get("name") or "nuestro restaurante"
        )
        feats = _features_dict(restaurant.get("features"))
        currency = feats.get("currency", "COP")
        sede_name = location.get("name") or restaurant_name
        sede_phone = location.get("phone") or ""

        token = f"web:{uuid.uuid4()}"
        await diner_sessions_repo.create_session(
            token=token,
            org_id=org_id,
            location_id=location_id,
            table_id=None,
            table_name=None,
            order_mode=body.order_mode,
        )

        turn = await _opening_turn(org_id, location_id, restaurant_name)

    log.info(
        "diner_session.opened",
        org_id=org_id,
        location_id=location_id,
        order_mode=body.order_mode,
    )

    return {
        "token": token,
        "org_id": org_id,
        "location_id": location_id,
        "table_id": None,
        "table_name": None,
        "restaurant_name": restaurant_name,
        # The ordering page (docs/claude/delivery-web.md chunk 5) needs the
        # SEDE'S own name/phone (not the org's) to show "te atenderá nuestra
        # sede X" and a "llamar al restaurante" affordance, plus the
        # already-resolved delivery config so the deterministic checkout form
        # never has to re-derive it (get_delivery_config stays the single
        # source of truth — the checkout endpoint re-resolves it again
        # server-side regardless, this is display-only).
        "sede_name": sede_name,
        "sede_phone": sede_phone,
        "payment_methods": cfg["payment_methods"],
        "delivery_fee": float(cfg["delivery_fee"]),  # JSON boundary
        "min_order": float(cfg["min_order"]),         # JSON boundary
        "currency": currency,
        "order_mode": body.order_mode,
        # False on a plan without the AI assistant: the UI hides the free-text box.
        "assistant": await plan_access.org_has_feature(org_id, plans.AI_ASSISTANT),
        "requires_join_code": False,
        "join_code": None,
        "message": turn["message"],
        "blocks": turn["blocks"],
    }


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
    open a session and does NOT show the greeting/menu — returns
    requires_join_code=True so the UI asks for the code and calls
    POST /api/diner/join. The diner_sessions row (token) is still created so
    that join call has something to resolve.

    order_mode=delivery|pickup (docs/claude/delivery-web.md chunk 2) is
    delegated to _create_delivery_pickup_session — a materially different
    flow (no table, a pre-resolved sede, Turnstile) that would only clutter
    this docstring/branch if inlined here.
    """
    ip = _client_ip(request)
    allowed = await state_store.rate_limit_check(
        f"diner_session:{ip}", max_requests=20, window_seconds=60,
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    if body.order_mode != "dine_in":
        return await _create_delivery_pickup_session(body, ip)

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
        if not restaurant:
            raise HTTPException(status_code=404, detail="Restaurante no configurado para esta mesa")

        # display_name says WHICH sede when the org has several (0092).
        restaurant_name = (
            restaurant.get("display_name") or restaurant.get("name") or "nuestro restaurante"
        )
        feats = _features_dict(restaurant.get("features"))
        currency = feats.get("currency", "COP")
        location_id_int = int(location_id) if location_id else None

        token = f"web:{uuid.uuid4()}"
        await diner_sessions_repo.create_session(
            token=token,
            org_id=org_id,
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
                "order_mode": "dine_in",
                "requires_join_code": True,
                "message": "",
                "blocks": [],
            }

        # Free table — open it, mint the join_code, and mark it verified:
        # web diners scanned the real physical QR, which is at least as
        # trustworthy as the WhatsApp geo-claim flow — no waiter hold.
        new_session = await tables_repo.db_create_table_session(
            token, org_id, table_id, table_name,
            location_id=location_id_int,
        )
        join_code = _generate_join_code()
        await tables_repo.db_set_session_join_code(new_session["id"], join_code)
        await tables_repo.db_mark_session_verified(new_session["id"])

        turn = await _opening_turn(org_id, location_id, restaurant_name, table_name)

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
        "order_mode": "dine_in",
        "assistant": await plan_access.org_has_feature(org_id, plans.AI_ASSISTANT),
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
        already = await db.db_get_active_session(token, org_id)
        if already:
            restaurant = await db.db_get_restaurant_by_org_id(org_id)
            restaurant_name = (restaurant or {}).get("name") or "nuestro restaurante"
            currency = _features_dict((restaurant or {}).get("features")).get("currency", "COP")
            turn = await _opening_turn(org_id, location_id, restaurant_name, table_name)
            return {**turn, "restaurant_name": restaurant_name, "currency": currency, "table_name": table_name,
                    "assistant": await plan_access.org_has_feature(org_id, plans.AI_ASSISTANT)}

        new_session = await tables_repo.db_link_participant_session(
            phone=token,
            org_id=org_id,
            table_id=table_id,
            table_name=table_name,
            join_code=code,
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

        restaurant = await db.db_get_restaurant_by_org_id(org_id)
        restaurant_name = (restaurant or {}).get("name") or "nuestro restaurante"
        currency = _features_dict((restaurant or {}).get("features")).get("currency", "COP")
        turn = await _opening_turn(org_id, location_id, restaurant_name, table_name)

    log.info("diner_join.success", org_id=org_id, table_id=table_id, session_id=new_session.get("id"))

    return {**turn, "restaurant_name": restaurant_name, "currency": currency, "table_name": table_name,
            "assistant": await plan_access.org_has_feature(org_id, plans.AI_ASSISTANT)}


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
            menu = await sede_menu.get_sede_menu(org_id, location_id)
            dishes = menu.get(category)
            if isinstance(dishes, list) and dishes:
                feats = _features_dict((await db.db_get_restaurant_by_org_id(org_id) or {}).get("features"))
                currency = feats.get("currency", "COP")
                # The diner is sitting at ONE sede — what that sede ran out
                # of is what greys out, not what some other sede ran out of.
                availability = await db.db_get_menu_availability(org_id, location_id)
                dish_block = _dish_cards_for_category(dishes, availability, currency)
                return {
                    "message": f"Esto es lo que tenemos en {category}:",
                    "blocks": [dish_block],
                }

        # Open the table session if this dine-in diner has none yet (first
        # message, or the previous sitting was closed) — from the table bound
        # to THIS diner session, never from message text. Rule #5 (cooldown):
        # not while another customer holds the table.
        table_id = session.get("table_id")
        if session.get("order_mode") == "dine_in" and table_id:
            active = await db.db_get_active_session(token, org_id)
            if not active:
                holder = await db.db_get_active_session_on_table_by_other_phone(table_id, token)
                table = await db.db_get_table_by_id(table_id)
                if holder or not table:
                    return {
                        "message": (
                            "Esta mesa ya está en uso por otro cliente. Si crees "
                            "que es un error, pídele al mesero que te ayude."
                        ),
                        "blocks": [],
                    }
                await db.db_create_table_session(
                    token, org_id, table["id"], table["name"],
                    location_id=table.get("location_id"),
                )

        result = await agent_chat(
            user_phone=token,
            user_message=user_message,
            org_id=org_id,
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
    """Full menu for the diner UI's 'Ver carta completa' panel."""
    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    location_id = session.get("location_id")

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        restaurant = await db.db_get_restaurant_by_org_id(org_id)
        # This sede's carta: its prices, without what it hides, plus its own
        # dishes (migration 0093).
        menu = await sede_menu.get_sede_menu(org_id, location_id)
        # Sold out is per sede (migration 0091): this session belongs to one.
        availability = await db.db_get_menu_availability(org_id, location_id)

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
    reason = body.reason

    message_text = _WAITER_MESSAGES.get(reason, _WAITER_MESSAGES["other"])
    table_name = session.get("table_name") or ""
    if table_name:
        message_text = f"{message_text} ({table_name})"

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        await db.db_create_waiter_alert(
            phone=token,
            org_id=org_id,
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

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        dish = await orders.resolve_dish_for_cart(
            org_id, sku=sku, name=name,
            location_id=session.get("location_id"),
        )
        if dish is None:
            raise HTTPException(
                status_code=404,
                detail="No encontramos ese plato o no está disponible en este momento",
            )

        result = await orders.add_cart_line(token, org_id, dish, body.qty, body.note)
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

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        result = await orders.update_cart_line(token, org_id, body.line_id, qty=body.qty, note=body.note)
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

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        result = await orders.remove_cart_line(token, org_id, body.line_id)
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

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)
        cart = await orders.get_cart_with_line_ids(token, org_id)
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
        active = await db.db_get_active_session(token, org_id)
        if not active:
            raise HTTPException(
                status_code=422,
                detail="Únete a la mesa con el código antes de enviar tu pedido.",
            )

        try:
            async with orders._cart_lock(token, org_id):
                # Re-check the cache inside the lock — a concurrent duplicate
                # request may have just finished and populated it.
                cached = await state_store.order_send_result_get(cache_key)
                if cached:
                    return cached

                cart = await db.db_get_cart(token, org_id)
                cart_items = cart.get("items") or []
                if not cart_items:
                    raise HTTPException(status_code=422, detail="Tu pedido está vacío")

                cart_total = await orders.get_cart_total(token, org_id)
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

                inv = await deduct_inventory_or_cancel(
                    org_id, cart_items, commit["order_id"], location_id=location_id,
                )
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
                await db.db_clear_cart(token, org_id)
                await tables_repo.db_session_mark_order(token, org_id)
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
    location_id = session.get("location_id")
    table_id = session.get("table_id")

    with tenant_scope(org_id):
        active = await db.db_get_active_session(token, org_id)
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


# ── Checkout — "Pedir la cuenta" mediated by the waiter (no gateway) ────────
#
# CLAUDE.md launch decision: no Wompi/Bold, no payment link. The diner
# declares WHAT they want to pay (scope) and HOW (method); the waiter
# charges on whatever datáfono the restaurant already has, or takes cash,
# and caja/mesero marks it paid through the EXISTING pay_check flow
# (app/routes/tables.py). This endpoint only creates the check + proposal +
# waiter_alerts hint — it never touches money itself.
#
# Item-level dedup without a schema change: each item we copy into a check
# carries an additive "order_id" key (the source table_orders row's id).
# Nothing else in the codebase reads/writes that key, so this is purely
# additive over the existing free-form table_checks.items JSONB shape. Any
# table_orders row whose id already appears inside an open/paying/invoiced
# check's items is "claimed" and can never be pulled into a second check —
# this is what makes "an item can't be charged twice" hold even though
# table_checks has no direct FK to table_orders.
def _claimed_order_ids(checks: list) -> set:
    claimed = set()
    for c in checks:
        if c.get("status") not in ("open", "paying", "invoiced"):
            continue
        for it in _parse_items_list(c.get("items")):
            if isinstance(it, dict) and it.get("order_id"):
                claimed.add(it["order_id"])
    return claimed


def _checkout_amount_label(amount: Decimal, currency: str) -> str:
    return format_money_es(amount, currency)


@router.post("/checkout")
async def diner_checkout(request: Request, body: DinerCheckoutRequest):
    """Diner taps 'Pedir la cuenta' → chooses scope (mine/table) + method
    (card/cash) [+ optional tip, name, phone] → we build a real check from
    THIS diner's (or the table's remaining) actual `table_orders` items,
    attach a `web_chat` payment proposal, and fire a waiter_alerts hint.
    No gateway, no payment link — see module docstring above.
    """
    ip = _client_ip(request)
    if not await state_store.rate_limit_check(f"diner_checkout_ip:{ip}", max_requests=20, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    token = body.token.strip()
    if not await state_store.rate_limit_check(f"diner_checkout:{token}", max_requests=5, window_seconds=30):
        raise HTTPException(status_code=429, detail="Espera un momento antes de pedir la cuenta de nuevo.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    location_id = session.get("location_id")
    table_id = session.get("table_id")
    table_name = session.get("table_name") or table_id
    if session.get("order_mode") != "dine_in" or not table_id:
        raise HTTPException(status_code=422, detail="Esta sesión no está asociada a una mesa")

    with tenant_scope(org_id):
        await diner_sessions_repo.touch_last_seen(token, org_id)

        active = await db.db_get_active_session(token, org_id)
        if not active:
            raise HTTPException(
                status_code=422,
                detail="Únete a la mesa con el código antes de pedir la cuenta.",
            )

        base_order_id = await tables_repo.db_get_base_order_id(table_id)
        if not base_order_id:
            raise HTTPException(status_code=422, detail="Todavía no has hecho ningún pedido en esta mesa.")

        currency = await _currency_for_org(org_id)

        lock_token = await state_store.table_checkout_lock_acquire(base_order_id)
        if lock_token is None:
            raise HTTPException(
                status_code=409,
                detail="Ya se está procesando un cobro para esta mesa, espera un momento.",
            )

        try:
            existing_checks = await tables_repo.db_get_checks(base_order_id)

            # Idempotent replay: this SAME diner already has an outstanding
            # (unpaid, unresolved) proposal — a double tap must not create a
            # second check or a second waiter_alerts row.
            own_pending = [
                c for c in existing_checks
                if c.get("proposal_customer_phone") == token
                and c.get("status") == "open"
                and c.get("proposal_status") in ("pending", "awaiting_proof", "proof_received")
            ]
            if own_pending:
                c = own_pending[0]
                subtotal_prev = to_decimal(c.get("subtotal") or c.get("total") or 0)
                tip_prev = to_decimal(c.get("tip_amount") or c.get("proposed_tip") or 0)
                return {
                    "success": True,
                    "check_id": c.get("id"),
                    "base_order_id": base_order_id,
                    "scope": body.scope,
                    "method": body.method,
                    "status": "pending_waiter",
                    "subtotal": float(quantize_money(subtotal_prev, currency)),  # JSON boundary
                    "tip_amount": float(quantize_money(tip_prev, currency)),  # JSON boundary
                    "total": float(quantize_money(subtotal_prev + tip_prev, currency)),  # JSON boundary
                    "currency": currency,
                    "message": "Ya le avisamos al mesero, ya viene con tu cuenta.",
                }

            claimed_ids = _claimed_order_ids(existing_checks)

            rows = await tables_repo.db_get_table_orders_by_base_id(base_order_id, branch_id=location_id)
            billable_rows = [
                r for r in rows
                if r.get("status") not in ("cancelado", "cancelled") and r.get("id") not in claimed_ids
            ]
            if body.scope == "mine":
                billable_rows = [r for r in billable_rows if r.get("phone") == token]
                empty_message = "No tienes pedidos pendientes de pago."
            else:
                empty_message = "La mesa no tiene saldo pendiente por cobrar."

            if not billable_rows:
                raise HTTPException(status_code=422, detail=empty_message)

            item_payload = []
            for row in billable_rows:
                order_id = row.get("id")
                for it in _parse_items_list(row.get("items")):
                    if not isinstance(it, dict):
                        continue
                    try:
                        qty = int(it.get("quantity") or it.get("qty") or 1)
                    except (ValueError, TypeError):
                        qty = 1
                    unit_price = to_decimal(it.get("price") if it.get("price") is not None else it.get("unit_price", 0))
                    raw_subtotal = it.get("subtotal")
                    line_subtotal = quantize_money(
                        to_decimal(raw_subtotal) if raw_subtotal is not None else money_mul(unit_price, qty),
                        currency,
                    )
                    item_payload.append({
                        "name": it.get("name", ""),
                        "qty": qty,
                        "unit_price": float(unit_price),  # JSON boundary
                        "subtotal": float(line_subtotal),  # JSON boundary
                        "order_id": order_id,
                    })

            # Row totals (already computed + trusted at order-send time), not
            # a re-sum of items — avoids drift against what the kitchen/mesero
            # screens already show for these same rows.
            subtotal_total = quantize_money(
                money_sum(to_decimal(r.get("total") or 0) for r in billable_rows), currency,
            )
            if subtotal_total <= ZERO:
                raise HTTPException(status_code=422, detail="El saldo a cobrar es cero.")

            tip_d = quantize_money(to_decimal(body.tip_amount), currency)
            if tip_d < ZERO:
                tip_d = ZERO
            if tip_d > money_mul(subtotal_total, Decimal("0.5")):
                raise HTTPException(status_code=400, detail="La propina no puede superar el 50% del total")

            new_check_number = max([c.get("check_number") or 0 for c in existing_checks], default=0) + 1
            new_check = await tables_repo.db_insert_check(
                base_order_id, new_check_number, item_payload,
                subtotal_total, ZERO, subtotal_total,
            )
            if new_check is None:
                # check_number collided with a concurrent writer despite our
                # own lock (e.g. an external caller outside this endpoint) —
                # never silently overwrite; surface as "try again".
                raise HTTPException(
                    status_code=409,
                    detail="La mesa cambió justo ahora, por favor intenta de nuevo.",
                )

            method_label = _CHECKOUT_METHOD_LABELS[body.method]
            await tables_repo.db_attach_proposal(
                check_id=new_check["id"],
                proposed_payments=[{"method": method_label, "amount": float(subtotal_total)}],  # JSON boundary
                proposed_tip=float(tip_d),  # JSON boundary
                proposal_source="web_chat",
                proposal_status="pending",
                customer_phone=token,
            )
            if tip_d > ZERO:
                await tables_repo.db_set_check_tip(new_check["id"], float(tip_d))  # JSON boundary

            total_to_collect = quantize_money(subtotal_total + tip_d, currency)

            scope_label = "lo suyo" if body.scope == "mine" else "toda la mesa"
            extra_bits = []
            if body.customer_name and body.customer_name.strip():
                extra_bits.append(f"Nombre: {body.customer_name.strip()[:100]}")
            if body.customer_phone and body.customer_phone.strip():
                extra_bits.append(f"Tel: {body.customer_phone.strip()[:30]}")
            extra_txt = f" ({'; '.join(extra_bits)})" if extra_bits else ""
            alert_message = (
                f"Mesa {table_name}: cobrar {scope_label} — "
                f"{_checkout_amount_label(total_to_collect, currency)} en "
                f"{_CHECKOUT_METHOD_DISPLAY[body.method]}{extra_txt}."
            )
            try:
                await tables_repo.db_create_waiter_alert(
                    phone=token, org_id=org_id, alert_type="bill",
                    message=alert_message, table_id=table_id, table_name=table_name,
                    location_id=location_id,
                )
            except Exception:
                # NO-ROMPER #17: a failed alert is a hint lost, not a reason
                # to fail the checkout the diner already committed to.
                log.exception("diner_checkout.waiter_alert_failed", org_id=org_id, table_id=table_id)
        finally:
            await state_store.table_checkout_lock_release(base_order_id, lock_token)

    response = {
        "success": True,
        "check_id": new_check["id"],
        "base_order_id": base_order_id,
        "scope": body.scope,
        "method": body.method,
        "status": "pending_waiter",
        "subtotal": float(subtotal_total),  # JSON boundary
        "tip_amount": float(tip_d),  # JSON boundary
        "total": float(total_to_collect),  # JSON boundary
        "currency": currency,
        "message": "Ya le avisamos al mesero, ya viene con tu cuenta.",
    }
    log.info(
        "diner_checkout.success", org_id=org_id, table_id=table_id,
        scope=body.scope, method=body.method, check_id=new_check["id"],
    )
    return response


@router.get("/status")
async def diner_status(token: str = Query(..., min_length=1, max_length=200)):
    """Lightweight, frequently-polled status surface — payment state
    ('pending_waiter' → 'paid') and, once the table's checks are fully
    invoiced, the NPS survey block (see app/services/blocks.py
    build_nps_prompt_block). Intentionally separate from GET /api/diner/table
    (which returns the full order list and is only fetched while that panel
    is open) so this can be polled globally without pulling the whole ticket
    every few seconds.
    """
    if not await state_store.rate_limit_check(f"diner_status:{token}", max_requests=60, window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta más tarde.")

    session = await _resolve_session_or_404(token)
    org_id = int(session["org_id"])
    location_id = session.get("location_id")
    table_id = session.get("table_id")

    with tenant_scope(org_id):
        currency = await _currency_for_org(org_id)
        # NOT db_get_base_order_id — that one deliberately excludes
        # 'factura_entregada' (it answers "what group should a NEW round
        # attach to"), which would make a just-fully-paid table's checkout
        # status vanish back to null right when the diner most wants to see
        # "paid". This is a read-only status view — see
        # db_get_latest_base_order_id_for_table's docstring.
        base_order_id = await tables_repo.db_get_latest_base_order_id_for_table(table_id) if table_id else None

        checkout_status = None
        if base_order_id:
            rows = await tables_repo.db_get_table_orders_by_base_id(base_order_id, branch_id=location_id)
            my_order_ids = {
                r.get("id") for r in rows
                if r.get("phone") == token and r.get("status") not in ("cancelado", "cancelled")
            }
            if my_order_ids:
                checks = await tables_repo.db_get_checks(base_order_id)
                best = None
                for c in checks:
                    if c.get("status") not in ("open", "paying", "invoiced"):
                        continue
                    covers_mine = any(
                        isinstance(it, dict) and it.get("order_id") in my_order_ids
                        for it in _parse_items_list(c.get("items"))
                    )
                    if not covers_mine:
                        continue
                    if c.get("status") == "invoiced":
                        best = c
                        break
                    if best is None:
                        best = c
                if best is not None:
                    amount = to_decimal(best.get("total") or 0) + to_decimal(best.get("tip_amount") or 0)
                    checkout_status = {
                        "check_id": best.get("id"),
                        "status": "paid" if best.get("status") == "invoiced" else "pending_waiter",
                        "amount": float(quantize_money(amount, currency)),  # JSON boundary
                        "currency": currency,
                    }

        nps_state = await state_store.nps_get(token, org_id)
        nps_block = None
        if nps_state and nps_state.get("state") in ("waiting_score", "waiting_comment"):
            stage = "comment" if nps_state.get("state") == "waiting_comment" else "score"
            nps_block = blocks.build_nps_prompt_block(stage)

    return {"checkout": checkout_status, "nps": nps_block}


async def _resolve_diner_stream_session(request: Request) -> dict:
    """Same diner auth as GET /api/diner/status (diner_sessions_repo.get_by_token),
    but the token comes from the Authorization header instead of the URL —
    per the SSE contract, tokens never go in the URL for the stream endpoint
    (EventSource can't send headers, so the browser uses fetch() + ReadableStream
    instead — see app/static/js/mesio-realtime.js). 401 (not 404) on any
    failure, matching GET /api/staff/stream's auth-gate shape.
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="Autenticación requerida")
    session = await diner_sessions_repo.get_by_token(token)
    if session is None:
        raise HTTPException(status_code=401, detail="Sesión no encontrada o expirada")
    return session


def _make_diner_filter(table_id: str | None, delivery_order_ids: frozenset[str] | None = None):
    """Build the per-connection topic_filter for GET /api/diner/stream.

    Standalone (module-level) so it's directly unit-testable — see
    tests/test_realtime.py — without spinning up a request. `resync` is
    NOT special-cased here: app.services.realtime.event_stream() already
    lets `resync` bypass any topic_filter unconditionally.

    Two branches, per docs/claude/delivery-web.md ("Realtime — two
    defects, both verified by the lead"):

    1. "delivery_order.updated" — a delivery/pickup session (table_id is
       always None for these — see diner_sessions_repo's order_mode
       convention) is scoped to entity_id membership in
       `delivery_order_ids` (the set of order ids THIS token owns, computed
       once at stream-open time — see delivery_repo.db_get_order_ids_for_
       phone). An empty/None set matches nothing, never "any" — the
       previous bug let a None table_id match ANY event published without
       one (`event.table_id == table_id` with both sides None), which
       would have handed every delivery customer of the org every other
       delivery customer's invalidation events.
    2. Every other allowlisted topic (dine-in: table_order.*, check.updated,
       nps.updated) requires table_id to be a real, non-None value that
       equals the event's own table_id — `table_id is not None` is the
       actual fix: a delivery/pickup session (table_id=None) can now never
       match a table-scoped event even if that event's own table_id also
       happened to be None.
    """
    def _filter(event: dict) -> bool:
        topic = event.get("topic")
        if topic not in realtime.DINER_ALLOWLIST:
            return False
        if topic == "delivery_order.updated":
            return bool(delivery_order_ids) and event.get("entity_id") in delivery_order_ids
        return table_id is not None and event.get("table_id") == table_id
    return _filter


@router.get("/stream")
async def diner_stream(request: Request):
    """Real-time SSE feed for the diner's own table OR the diner's own
    delivery/pickup order(s) (docs/claude/delivery-web.md chunk 6, "Customer
    status page").

    Streams only the allowlisted topics for the diner's own scope — see
    app.services.realtime.DINER_ALLOWLIST and _make_diner_filter's docstring
    for exactly how "own" is enforced for each case. `resync` events always
    pass through regardless of the filter.
    """
    session = await _resolve_diner_stream_session(request)
    org_id = int(session["org_id"])
    table_id = session.get("table_id")

    delivery_order_ids: frozenset[str] | None = None
    if session.get("order_mode") in ("delivery", "pickup"):
        with tenant_scope(org_id):
            owned_ids = await delivery_repo.db_get_order_ids_for_phone(org_id, session.get("token") or "")
        delivery_order_ids = frozenset(owned_ids)

    return StreamingResponse(
        realtime.event_stream(
            request.is_disconnected, org_id,
            topic_filter=_make_diner_filter(table_id, delivery_order_ids),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
