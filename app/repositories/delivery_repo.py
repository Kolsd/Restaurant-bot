"""
app/repositories/delivery_repo.py
==================================
SQL layer for the web delivery/pickup wave (docs/claude/delivery-web.md).

Scope notes (mirrors diner_sessions_repo.py / qr_claims_repo.py):
  - db_get_org_by_slug() and db_get_order_by_public_code() are PRE-tenant
    lookups: the public entry point (a slug in the URL, or a public code on
    the customer's status page) does not know org_id yet — that is exactly
    what these functions resolve. Both run under bypass_tenant_scope(), same
    pattern as diner_sessions_repo.get_by_token().
  - Every other function assumes the CALLER has already entered
    tenant_scope(org_id) (the standard repo convention in this codebase —
    see diner_sessions_repo.touch_last_seen / loyalty_repo.* — the org_id
    parameter is used to build/filter the query, not to open the scope).
  - `orders` has RLS ENABLE + FORCE (migration 0076). `locations` and
    `organizations` do NOT (they are not in _RLS_TABLES — see
    docs/claude/rls-multitenant.md), so every query against them filters by
    org_id explicitly in the WHERE clause; RLS is not there to help.
  - jsonb parameters: the pool's jsonb codec (app/services/database.py,
    encoder=json.dumps) already serializes Python dicts/lists for a
    `$n::jsonb` parameter. Passing a PRE-dumped json.dumps() string here
    would double-encode it (see the P0 note in inventory_repo.py) — so raw
    dicts are passed directly, never json.dumps()'d, and Decimal values are
    converted to JSON-safe primitives (float) before the dict crosses into
    the jsonb column, since Decimal is not JSON-serializable.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from app.repositories.orders_repo import InsufficientStockError, deduct_inventory_in_tx
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)

# Unambiguous alphabet: no O/0, no I/1.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 6
_MAX_CODE_ATTEMPTS = 12

# Order lifecycle vocabulary reused from the WhatsApp era (see
# docs/claude/delivery-web.md — "Order lifecycle").
STATUS_PENDING_ACCEPTANCE = "pendiente_aceptacion"
STATUS_IN_PREPARATION = "en_preparacion"
STATUS_READY = "listo"
STATUS_REJECTED = "rechazado"
STATUS_CANCELLED = "cancelado"
STATUS_ON_THE_WAY = "en_camino"
STATUS_AT_THE_DOOR = "en_puerta"
STATUS_DELIVERED = "entregado"

_TERMINAL_STATUSES = (STATUS_REJECTED, STATUS_CANCELLED, STATUS_DELIVERED)

# Statuses from which the cashier may mark an order "en route" (chunk 4) —
# either straight out of the kitchen (en_preparacion) or after the kitchen
# has separately marked it "listo" via the existing KDS screen
# (app/routes/tables.py PATCH /api/kitchen/delivery-orders/{id}/status).
_PRE_EN_ROUTE_STATUSES = (STATUS_IN_PREPARATION, STATUS_READY)

_ORDER_FIELDS = """
    id, org_id, location_id, phone, bot_number, order_type, status,
    address, notes, items, subtotal, delivery_fee, total, paid,
    paid_at, paid_by_staff_id, payment_method, proof_url, channel,
    public_code, customer_name, customer_phone, customer_email,
    delivery_lat, delivery_lon, tip_amount, cash_change_for,
    accepted_at, accepted_by_staff_id, estimated_minutes, eta_communicated,
    rejected_at, rejection_reason,
    cancelled_at, cancelled_reason,
    courier_staff_id, courier_assigned_at, delivered_at,
    scheduled_pickup_at, nps_answered_at, created_at
"""


def _json_safe(value: Any) -> Any:
    """Recursively convert Decimal -> float so a dict is safe to hand to the
    jsonb codec.  # JSON boundary
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _random_code() -> str:
    """One candidate public code. Module-level so tests can force a collision
    by monkeypatching this function directly."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


# ── Location delivery config ─────────────────────────────────────────────


async def db_set_location_delivery_config(org_id: int, location_id: int, config: dict) -> Optional[dict]:
    """Overwrite a location's delivery_config. Returns the updated row, or
    None if no location with that id belongs to org_id.

    # Requires active tenant_scope(org_id) — org must already be resolved.
    """
    safe_config = _json_safe(config or {})
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE locations
            SET delivery_config = $1::jsonb, updated_at = NOW()
            WHERE id = $2 AND org_id = $3
            RETURNING id, org_id, delivery_config
            """,
            safe_config, location_id, org_id,
        )
    if not row:
        return None
    log.info("delivery.location_config_set", org_id=org_id, location_id=location_id)
    return dict(row)


async def db_get_org_locations_for_entry(org_id: int) -> list[dict]:
    """Active locations of an org, WITH everything the public entry point's
    sede-assignment ladder (app/services/delivery.py) needs: GPS, hours,
    timezone, delivery_config, and the customer-facing phone (migration
    0083). Deliberately NOT restaurant_repo.db_get_org_locations() — that
    getter does not select delivery_config or phone, and this wave must not
    re-read the delivery_config JSONB anywhere except through
    get_delivery_config() (docs/claude/delivery-web.md).

    Never selects whatsapp_number / wa_phone_id / wa_access_token — those
    must not reach the public /api/diner/org/{slug} response.

    # Requires active tenant_scope(org_id) — org must already be resolved
    # (mirrors every other function in this module except the two pre-tenant
    # lookups above).
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, name, address, phone, latitude, longitude,
                   timezone, opening_hours, delivery_config
            FROM locations
            WHERE org_id = $1 AND active = true
            ORDER BY name ASC, id ASC
            """,
            org_id,
        )
    return [dict(r) for r in rows]


async def db_get_location_delivery_config(org_id: int, location_id: int) -> dict:
    """Return the raw delivery_config dict for a location ({} if unset or
    if the location does not belong to org_id).

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT delivery_config FROM locations WHERE id = $1 AND org_id = $2",
            location_id, org_id,
        )
    if not row:
        return {}
    return row["delivery_config"] or {}


# ── Pre-tenant public lookups (bypass_tenant_scope) ──────────────────────


async def db_get_org_by_slug(slug: str) -> Optional[dict]:
    """Resolve an organization from its public slug (`/pedir/{slug}`).

    Pre-tenant lookup — mirrors diner_sessions_repo.get_by_token().
    """
    if not slug:
        return None
    with bypass_tenant_scope("delivery_org_slug_lookup"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, slug, features FROM organizations WHERE slug = $1",
                slug,
            )
    return dict(row) if row else None


async def db_get_order_by_public_code(public_code: str) -> Optional[dict]:
    """Resolve an order from its public status-page code (`/pedido/{code}`).

    Pre-tenant lookup — the customer's status URL carries no org_id.

    public_code is GLOBALLY unique (ux_orders_public_code, migration 0082),
    so this lookup resolves exactly one order across every tenant. It must
    stay that way: a per-org code space would make this function return
    another tenant's order on a collision — the same bug class as the
    removed `db_get_restaurant_by_id`.
    """
    if not public_code:
        return None
    with bypass_tenant_scope("delivery_public_code_lookup"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                f"SELECT {_ORDER_FIELDS} FROM orders WHERE public_code = $1",
                public_code,
            )
    return dict(row) if row else None


async def db_claim_public_code(order_id: str, org_id: int) -> str:
    """Mint a short public code for an existing order and claim it atomically.

    The code space is GLOBALLY unique (ux_orders_public_code, migration
    0082) because the customer's `/pedido/{code}` URL carries no org_id.
    That makes a pre-check impossible from inside a tenant: `orders` has
    RLS FORCE, so `SELECT ... WHERE public_code = $1` under
    tenant_scope(org_id) is blind to every other tenant's codes, and
    bypass_tenant_scope() cannot be entered while a tenant is pinned
    (TenantContextConflict) — which the caller always is, since it just
    created the order.

    So the unique INDEX is the guarantee, not a read: each attempt claims a
    candidate inside its own savepoint and a UniqueViolation simply means
    "someone, in any tenant, already has this code" — retry. That is also
    race-free against a concurrent worker, which a read-then-write check
    would not be.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        for _attempt in range(_MAX_CODE_ATTEMPTS):
            candidate = _random_code()
            try:
                async with conn.transaction():
                    claimed = await conn.fetchval(
                        """UPDATE orders SET public_code = $1
                            WHERE id = $2 AND org_id = $3
                        RETURNING public_code""",
                        candidate, order_id, org_id,
                    )
            except asyncpg.exceptions.UniqueViolationError:
                continue
            if claimed is None:
                raise ValueError(
                    f"db_claim_public_code: order {order_id} not found in org {org_id}"
                )
            return claimed
    raise RuntimeError(
        f"db_claim_public_code: exhausted {_MAX_CODE_ATTEMPTS} attempts "
        f"for order_id={order_id}"
    )


# ── Order creation (chunk 3, docs/claude/delivery-web.md) ──────────────────


async def db_create_delivery_order(
    *,
    org_id: int,
    location_id: int,
    phone: str,
    bot_number: str,
    order_type: str,
    items: list,
    address: str,
    subtotal: Decimal,
    delivery_fee: Decimal,
    tip_amount: Decimal,
    total: Decimal,
    payment_method: str,
    cash_change_for: Optional[Decimal],
    customer_name: str,
    customer_phone: str,
    customer_email: Optional[str],
    delivery_lat: Optional[float],
    delivery_lon: Optional[float],
    proof_url: Optional[str],
    scheduled_pickup_at: Optional[datetime],
    channel: str = "web_chat",
    notes: str = "",
) -> dict:
    """Create ONE new delivery/pickup order row, always status
    pendiente_aceptacion and paid=False (docs/claude/delivery-web.md, "Order
    lifecycle"). Always inserts a NEW row — idempotency (same
    idempotency_key from the same diner token must return the SAME order) is
    the CALLER's responsibility via a state_store cache keyed by
    token:idempotency_key, mirroring app/routes/diner.py's /order/send.
    This function has no idea what an idempotency_key is.

    org_id/location_id are trusted here — the caller (the checkout route)
    must have already validated the location belongs to org_id and is
    actually open/covering the order_mode before calling this.

    items is a raw Python list (cart items) — passed through _json_safe so a
    Decimal subtotal on any line can't blow up the jsonb codec.

    Inventory deduction (chunk 4, docs/claude/delivery-web.md): a web
    delivery/pickup order deducts inventory exactly like a table order does,
    reusing orders_repo.deduct_inventory_in_tx (made public in chunk 4 for
    exactly this reuse — see its docstring) against the SAME connection as
    the INSERT below. tenant_connection() already opens a transaction (see
    app/services/tenant_db.py), so an InsufficientStockError raised by the
    deduction rolls back the INSERT too — the order row is never created for
    a dish that cannot actually be fulfilled, unlike the dine-in path (which
    saves the table_order row first and cancels it afterwards on shortage —
    table_orders has no equivalent all-or-nothing requirement since a
    cancelled row there stays visible-but-filtered rather than absent).
    Raises InsufficientStockError — the caller (the checkout route) turns it
    into a 422 with a Spanish message, same shape as every other checkout
    refusal.

    # Requires active tenant_scope(org_id).
    """
    order_id = f"WEB-{uuid.uuid4().hex[:12].upper()}"
    safe_items = _json_safe(items or [])
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO orders (
                id, org_id, location_id, phone, bot_number, order_type, status,
                address, notes, items, subtotal, delivery_fee, tip_amount, total,
                paid, payment_method, cash_change_for, customer_name,
                customer_phone, customer_email, delivery_lat, delivery_lon,
                proof_url, scheduled_pickup_at, channel
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10::jsonb, $11, $12, $13, $14,
                $15, $16, $17, $18,
                $19, $20, $21, $22,
                $23, $24, $25
            )
            RETURNING {_ORDER_FIELDS}
            """,
            order_id, org_id, location_id, phone, bot_number, order_type, STATUS_PENDING_ACCEPTANCE,
            address, notes, safe_items, subtotal, delivery_fee, tip_amount, total,
            False, payment_method, cash_change_for, customer_name,
            customer_phone, customer_email, delivery_lat, delivery_lon,
            proof_url, scheduled_pickup_at, channel,
        )
        if items:
            # The sede that will cook it — stock is per sede (PM 2026-09-20).
            await deduct_inventory_in_tx(conn, org_id, items, location_id=location_id)
    log.info(
        "delivery.order_created",
        org_id=org_id, location_id=location_id, order_id=order_id, order_type=order_type,
    )
    return dict(row)


async def db_get_order_ids_for_phone(org_id: int, phone: str, limit: int = 20) -> list[str]:
    """Order ids this diner-session TOKEN (the `orders.phone` routing key,
    NOT the customer's real phone number — see the module docstring's
    "web:<uuid4>" convention) has created in this org, most recent first.

    Used ONLY to scope the diner SSE stream's delivery-order topic filter
    (app/routes/diner.py::_make_diner_filter / diner_stream) to orders the
    connecting token actually owns — realtime events carry no personal data
    to filter on directly (docs/claude/delivery-web.md, "Realtime — two
    defects"), so the filter needs its own allowlist of entity_ids computed
    once when the stream opens.

    NOT an authorization check by itself: the actual cancel/NPS endpoints
    re-read the order's own `phone` column directly on every call
    (app/routes/diner_delivery.py) rather than trusting this list, which is
    only ever used to decide whether to forward an already-content-free
    invalidation event to a live connection.

    # Requires active tenant_scope(org_id).
    """
    if not phone:
        return []
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            "SELECT id FROM orders WHERE org_id = $1 AND phone = $2 ORDER BY created_at DESC LIMIT $3",
            org_id, phone, limit,
        )
    return [r["id"] for r in rows]


async def db_count_open_orders_for_phone(org_id: int, customer_phone: str) -> int:
    """Count this org's orders for customer_phone that are still "open" — not
    yet in a terminal status (docs/claude/delivery-web.md: "an order is open
    until entregado, rechazado or cancelado"). Used for the per-phone cap on
    simultaneous open orders (anti-abuse, chunk 3).

    # Requires active tenant_scope(org_id).
    """
    if not customer_phone:
        return 0
    async with tenant_connection() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM orders
            WHERE org_id = $1 AND customer_phone = $2 AND status != ALL($3::text[])
            """,
            org_id, customer_phone, list(_TERMINAL_STATUSES),
        )
    return int(count or 0)


# ── Listing ───────────────────────────────────────────────────────────────


async def db_list_delivery_orders(
    org_id: int, location_id: int, statuses: Optional[list[str]] = None
) -> list[dict]:
    """List delivery/pickup orders for ONE sede. Always filtered by
    location_id — never org-wide (docs/claude/delivery-web.md).

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if statuses:
            rows = await conn.fetch(
                f"""
                SELECT {_ORDER_FIELDS} FROM orders
                WHERE org_id = $1 AND location_id = $2 AND status = ANY($3::text[])
                ORDER BY created_at DESC
                """,
                org_id, location_id, list(statuses),
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT {_ORDER_FIELDS} FROM orders
                WHERE org_id = $1 AND location_id = $2
                ORDER BY created_at DESC
                """,
                org_id, location_id,
            )
    return [dict(r) for r in rows]


async def db_get_delivery_order_for_sede(org_id: int, location_id: int, order_id: str) -> Optional[dict]:
    """Fetch ONE order iff it belongs to this org AND this sede — no status
    filter. Used by the cashier routes (chunk 4) as a pure OWNERSHIP check,
    separate from whatever status-transition guard runs next: a caller who
    can't even see this order (wrong org, or same org but wrong sede) gets a
    404 ("not found" — the same "doesn't exist for you" treatment the rest
    of this codebase gives a cross-tenant id, e.g.
    app/routes/diner_delivery.py::_resolve_org_or_404 and
    app/routes/deps.py::get_current_location's unowned-location 404/403),
    while a caller who legitimately owns the order but hits an illegal
    status transition (accept twice, reject after acceptance, ...) gets a
    409 Conflict from the transition call itself — a real state conflict on
    a resource they ARE allowed to see, which is the same distinction the
    existing `cart_lock_contention` -> 409 convention already draws
    elsewhere in this codebase. Folding both cases into one status code
    would leak "this order exists, just not for you" information through a
    409 that HTTP reserves for state conflicts, not authorization.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_ORDER_FIELDS} FROM orders
            WHERE id = $1 AND org_id = $2 AND location_id = $3
            """,
            order_id, org_id, location_id,
        )
    return dict(row) if row else None


# ── Status transitions ────────────────────────────────────────────────────


# `location_id` is OPTIONAL (default None = no sede filter, org-wide) on every
# transition below so tests/test_delivery_repo.py's existing chunk-1 calls
# (org-only) keep working unchanged. Chunk 4's cashier routes
# (app/routes/staff_delivery.py) always pass it — "staff must only ever see
# and act on their own location's orders" (docs/claude/delivery-web.md) is a
# SQL WHERE guarantee here, not just a filter on the list endpoint.


async def db_accept_order(
    org_id: int, order_id: str, staff_id: str, eta_minutes: int,
    location_id: Optional[int] = None,
) -> Optional[dict]:
    """Cashier accepts: sets accepted_at/accepted_by_staff_id/ETA and moves
    status to en_preparacion. Only from pendiente_aceptacion — enforced in
    the WHERE, not just in Python. Returns None (no row touched) otherwise.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $4, accepted_at = NOW(), accepted_by_staff_id = $3,
                    estimated_minutes = $5
                WHERE id = $1 AND org_id = $2 AND location_id = $7 AND status = $6
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, staff_id, STATUS_IN_PREPARATION, eta_minutes,
                STATUS_PENDING_ACCEPTANCE, location_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $4, accepted_at = NOW(), accepted_by_staff_id = $3,
                    estimated_minutes = $5
                WHERE id = $1 AND org_id = $2 AND status = $6
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, staff_id, STATUS_IN_PREPARATION, eta_minutes,
                STATUS_PENDING_ACCEPTANCE,
            )
    if row:
        log.info("delivery.order_accepted", org_id=org_id, order_id=order_id, staff_id=staff_id)
    return dict(row) if row else None


async def db_reject_order(
    org_id: int, order_id: str, reason: str, location_id: Optional[int] = None,
) -> Optional[dict]:
    """Cashier rejects with a reason. Only from pendiente_aceptacion —
    enforced in the WHERE. Returns None otherwise.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $3, rejected_at = NOW(), rejection_reason = $4
                WHERE id = $1 AND org_id = $2 AND location_id = $6 AND status = $5
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_REJECTED, reason, STATUS_PENDING_ACCEPTANCE, location_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $3, rejected_at = NOW(), rejection_reason = $4
                WHERE id = $1 AND org_id = $2 AND status = $5
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_REJECTED, reason, STATUS_PENDING_ACCEPTANCE,
            )
    if row:
        log.info("delivery.order_rejected", org_id=org_id, order_id=order_id)
    return dict(row) if row else None


async def db_cancel_order(org_id: int, order_id: str) -> Optional[dict]:
    """Customer cancels. ONLY while still pendiente_aceptacion — enforced in
    the SQL WHERE (never after acceptance). Returns None if the order has
    already moved past that status (or does not belong to org_id).

    Not location-scoped: the CUSTOMER cancels their own order via their
    diner_sessions token, which is already org+order scoped by the caller —
    there is no separate "staff sede" actor on this path.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE orders
            SET status = $3, cancelled_at = NOW()
            WHERE id = $1 AND org_id = $2 AND status = $4
            RETURNING {_ORDER_FIELDS}
            """,
            order_id, org_id, STATUS_CANCELLED, STATUS_PENDING_ACCEPTANCE,
        )
    if row:
        log.info("delivery.order_cancelled", org_id=org_id, order_id=order_id)
    return dict(row) if row else None


async def db_assign_courier(
    org_id: int, order_id: str, staff_id: str, location_id: Optional[int] = None,
) -> Optional[dict]:
    """Cashier assigns a rider. Refused once the order is in a terminal
    state (rechazado/cancelado/entregado) — enforced in the WHERE.

    Only checks the ORDER's own org/sede here — the caller
    (app/routes/staff_delivery.py) is responsible for validating that
    `staff_id` itself is a courier of the SAME org+sede before calling this
    (a separate lookup: a courier is not scoped by an order row).

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET courier_staff_id = $3, courier_assigned_at = NOW()
                WHERE id = $1 AND org_id = $2 AND location_id = $5 AND status != ALL($4::text[])
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, staff_id, list(_TERMINAL_STATUSES), location_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET courier_staff_id = $3, courier_assigned_at = NOW()
                WHERE id = $1 AND org_id = $2 AND status != ALL($4::text[])
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, staff_id, list(_TERMINAL_STATUSES),
            )
    if row:
        log.info("delivery.courier_assigned", org_id=org_id, order_id=order_id, staff_id=staff_id)
    return dict(row) if row else None


async def db_mark_en_route(
    org_id: int, order_id: str, location_id: Optional[int] = None,
) -> Optional[dict]:
    """Cashier marks the order en route (courier picked it up). Only from
    en_preparacion/listo — enforced in the WHERE — moving it to en_camino.

    Deliberately does NOT require a courier_staff_id to already be set:
    that would wrongly block PICKUP orders (order_type='recoger'), which
    have no rider at all, from ever reaching entregado through this same
    lifecycle. Courier assignment stays a separate, independent action.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $3
                WHERE id = $1 AND org_id = $2 AND location_id = $5 AND status = ANY($4::text[])
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_ON_THE_WAY, list(_PRE_EN_ROUTE_STATUSES), location_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE orders
                SET status = $3
                WHERE id = $1 AND org_id = $2 AND status = ANY($4::text[])
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_ON_THE_WAY, list(_PRE_EN_ROUTE_STATUSES),
            )
    if row:
        log.info("delivery.order_en_route", org_id=org_id, order_id=order_id)
    return dict(row) if row else None


async def db_mark_ready(
    org_id: int, order_id: str, location_id: Optional[int] = None,
) -> Optional[dict]:
    """Kitchen marks a web delivery/pickup order ready (`listo`). Only from
    `en_preparacion` — i.e. only after the cashier accepted it — enforced in
    the WHERE, so the kitchen can never jump a web order past acceptance or
    straight to delivered.

    location_id (chunk 8, "Known open items"): optional, defaults to None
    (no sede filter) so existing callers/tests keep working unchanged —
    mirrors every other transition in this module (db_accept_order,
    db_reject_order, ...). app/routes/tables.py passes it so a kitchen can
    never mark another sede's order ready.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            row = await conn.fetchrow(
                f"""
                UPDATE orders SET status = $3
                 WHERE id = $1 AND org_id = $2 AND location_id = $5 AND status = $4
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_READY, STATUS_IN_PREPARATION, location_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                UPDATE orders SET status = $3
                 WHERE id = $1 AND org_id = $2 AND status = $4
                RETURNING {_ORDER_FIELDS}
                """,
                order_id, org_id, STATUS_READY, STATUS_IN_PREPARATION,
            )
    if row:
        log.info("delivery.order_ready", org_id=org_id, order_id=order_id)
    return dict(row) if row else None


async def db_get_order_channel(org_id: int, order_id: str) -> Optional[dict]:
    """Just enough of an order to route it: channel, status, location_id.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT channel, status, location_id FROM orders WHERE id = $1 AND org_id = $2",
            order_id, org_id,
        )
    return dict(row) if row else None


async def db_claim_order_nps(org_id: int, order_id: str) -> bool:
    """Atomically record that the customer answered this order's survey
    (rated or skipped). True only for the FIRST call on a delivered order;
    any later or concurrent call gets False — the conditional UPDATE is the
    guard, so two simultaneous submits cannot both pass.

    Per ORDER, not per customer: the web session token is reused across
    orders, so a per-token flag let a returning customer rate only once ever.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        claimed = await conn.fetchval(
            """
            UPDATE orders SET nps_answered_at = NOW()
             WHERE id = $1 AND org_id = $2
               AND status = $3 AND nps_answered_at IS NULL
            RETURNING id
            """,
            order_id, org_id, STATUS_DELIVERED,
        )
    return claimed is not None


async def db_mark_delivered(
    org_id: int, order_id: str, location_id: Optional[int] = None,
) -> Optional[dict]:
    """Rider/cashier marks the order delivered (or collected, for pickup).

    Allowed FROM-states depend on the order type, enforced in the WHERE:
      - delivery ('domicilio'): only from en_camino / en_puerta — it must have
        left with a rider first.
      - pickup ('recoger'): from en_preparacion / listo. A pickup order never
        goes en_camino (there is no rider), so requiring it would leave every
        pickup order open forever — and an open order counts against the
        per-phone open-orders cap at checkout, eventually locking the customer
        out of ordering again.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE orders
            SET status = $3, delivered_at = NOW()
            WHERE id = $1 AND org_id = $2
              AND ($5::bigint IS NULL OR location_id = $5)
              AND (
                    (order_type = 'domicilio' AND status = ANY($4::text[]))
                 OR (order_type = 'recoger'   AND status = ANY($6::text[]))
              )
            RETURNING {_ORDER_FIELDS}
            """,
            order_id, org_id, STATUS_DELIVERED,
            [STATUS_ON_THE_WAY, STATUS_AT_THE_DOOR],
            location_id,
            [STATUS_IN_PREPARATION, STATUS_READY],
        )
    if row:
        log.info("delivery.order_delivered", org_id=org_id, order_id=order_id)
    return dict(row) if row else None


async def db_mark_order_paid(
    org_id: int,
    order_id: str,
    *,
    location_id: Optional[int] = None,
    payment_method: str,
    staff_id: Optional[str] = None,
) -> Optional[dict]:
    """Record that a web delivery/pickup order was paid, and by whom.

    This is the ONLY way a web-channel order reaches paid = TRUE. Before it
    existed, `paid` was written exclusively by orders_repo.db_confirm_payment
    from the Wompi webhook — which is switched off — so cash at the door, the
    rider's card reader and an uploaded transfer receipt all left the order
    unpaid forever, and out of the owner's sales totals (stats_repo sums
    `orders.total WHERE paid = TRUE`).

    Idempotent and non-reversible by design, enforced in the WHERE, not by a
    read-then-write the two tablets could interleave:
      - `paid = FALSE` — a second submit returns None instead of overwriting
        who collected the money or when.
      - status NOT IN (cancelado, rechazado) — an order nobody will serve
        must not be bookable as revenue.

    Returns the updated row, or None when no row qualified. The caller maps
    None to 409; it must NOT treat it as success, because "already paid" and
    "cancelled" are both reachable and the cashier needs to know which.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE orders
               SET paid = TRUE,
                   paid_at = NOW(),
                   paid_by_staff_id = $4::uuid,
                   payment_method = $5
             WHERE id = $1 AND org_id = $2
               AND ($3::bigint IS NULL OR location_id = $3)
               AND paid = FALSE
               AND NOT (status = ANY($6::text[]))
            RETURNING {_ORDER_FIELDS}
            """,
            order_id, org_id, location_id, staff_id, payment_method,
            [STATUS_CANCELLED, STATUS_REJECTED],
        )
    if row:
        log.info(
            "delivery.order_paid",
            org_id=org_id, order_id=order_id,
            payment_method=payment_method, by_staff=bool(staff_id),
        )
    return dict(row) if row else None


# ── Courier's own queue + roster (chunk 7) ──────────────────────────────────

# Role names (Spanish + legacy English alias) that grant the courier
# section — mirrors app/routes/staff_delivery.py::_COURIER_ROLES. Kept as a
# local tuple (not imported) to avoid a routes -> repositories import.
_COURIER_ROLE_NAMES = ("domiciliario", "delivery")


async def db_list_delivery_orders_for_courier(
    org_id: int, location_id: int, courier_staff_id: str
) -> list[dict]:
    """List delivery/pickup orders assigned to ONE courier, in ONE sede —
    the courier section's "my orders" feed (docs/claude/delivery-web.md,
    chunk 7). Never another courier's orders: filtered by courier_staff_id
    in the SQL, not just by what the frontend chooses to render.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_ORDER_FIELDS} FROM orders
            WHERE org_id = $1 AND location_id = $2 AND courier_staff_id = $3
            ORDER BY created_at DESC
            """,
            org_id, location_id, courier_staff_id,
        )
    return [dict(r) for r in rows]


async def db_list_couriers_for_sede(org_id: int, location_id: int) -> list[dict]:
    """Active staff with a courier role (domiciliario/delivery) in ONE sede
    — feeds the cashier's 'Asignar domiciliario' picker (chunk 7). Role
    matching mirrors app/routes/staff_delivery.py::_roles_from_staff_row:
    the `roles` jsonb array when present, else the single `role` column.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text, name
            FROM staff
            WHERE org_id = $1 AND location_id = $2 AND active = true
              AND (role = ANY($3::text[]) OR roles ?| $3::text[])
            ORDER BY name ASC
            """,
            org_id, location_id, list(_COURIER_ROLE_NAMES),
        )
    return [dict(r) for r in rows]


# ── Courier validation (chunk 4) ────────────────────────────────────────────


async def db_get_staff_for_courier_check(org_id: int, staff_id: str) -> Optional[dict]:
    """Fetch the fields needed to validate a courier assignment: the staff
    member must belong to the SAME org (enforced in the WHERE — RLS also
    covers this since `staff` is tenant-scoped), be active, and the caller
    checks role + location_id against the order's own sede. Returns None if
    no such staff row exists in this org at all.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text, org_id, location_id, role, roles, active
            FROM staff
            WHERE id = $1::uuid AND org_id = $2
            """,
            staff_id, org_id,
        )
    return dict(row) if row else None
