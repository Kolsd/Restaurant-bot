"""
app/services/table_order_commit.py
====================================
Shared "commit one round of table-order items to the kitchen/bar" core.

Used by BOTH:
  - The WhatsApp LLM path: app/services/agent_salon.py::execute_salon_action
    (action="order") — reached via Claude's place_order tool.
  - The Mesio-native diner web-chat path: app/routes/diner.py
    POST /api/diner/order/send — a deterministic REST call, no LLM involved.

Extracted (2026-09) so the kitchen/bar/mesero/caja screens see a web order
exactly like a bot order: same station routing (kitchen vs bar), same
base_order_id/sub_number numbering for multiple rounds at a table, same
inventory deduction with typed InsufficientStockError handling
(CLAUDE.md NO-ROMPER #13 — surfaced to the customer, never swallowed by a
generic except).

Does NOT own (stays with each caller):
  - Reading/clearing the cart.
  - Per-caller dedup: agent_salon.py's message-text heuristics (Capas 1-3,
    LLM-specific) vs. the diner path's client idempotency_key + cart lock.
  - Reply/response formatting.

Money: `cart_total` / station totals are Decimal end-to-end internally;
callers are responsible for JSON-boundary conversion when building an HTTP
response (CLAUDE.md "Capa Financiera Decimal").
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.services import database as db
from app.services.logging import get_logger
from app.services.money import money_mul, money_sum, to_decimal
from app.services.tenant_db import tenant_connection
from app.repositories.orders_repo import InsufficientStockError

log = get_logger(__name__)


def resolve_station_split(cart_items: list, features: dict | None) -> dict:
    """Split `cart_items` into kitchen vs bar buckets per
    `features.bar_enabled` / `features.bar_categories`. Mirrors the
    long-standing agent_salon.py routing rule verbatim.

    Returns {"kitchen_items", "bar_items", "has_split", "kitchen_station"}.
    `kitchen_station` is "kitchen" when there IS a bar split (so the KDS
    filters correctly), else "all" (single-station restaurant — every
    screen shows it).
    """
    features = features or {}
    bar_enabled = bool(features.get("bar_enabled", False))
    bar_categories = list(features.get("bar_categories", []) or [])

    if bar_enabled and bar_categories:
        kitchen_items = [i for i in cart_items if i.get("category", "") not in bar_categories]
        bar_items = [i for i in cart_items if i.get("category", "") in bar_categories]
    else:
        kitchen_items = list(cart_items)
        bar_items = []

    has_split = bool(kitchen_items) and bool(bar_items)
    return {
        "kitchen_items": kitchen_items,
        "bar_items": bar_items,
        "has_split": has_split,
        "kitchen_station": "kitchen" if has_split else "all",
    }


def station_total(items: list) -> Decimal:
    return money_sum(
        to_decimal(i.get("subtotal", money_mul(to_decimal(i.get("price", 0)), i.get("quantity", 1))))
        for i in items
    )


def _with_ticket_notes(items: list) -> list:
    """Map the cart's per-item `note` (singular — app/services/orders.py's
    cart line model) to `notes` (plural), the key the kitchen/bar screens
    actually render: app/static/js/pages/kitchen.js and
    app/static/js/pages/bar.js both read `item.notes`. Without this, a
    diner's "sin chicharrón" note is saved on the cart line and NEVER
    printed on the ticket — the two channels have quietly disagreed on this
    field name since per-item notes were added. Fixed once, here, in the
    single place both the WhatsApp and diner paths save a table_order round.

    Returns shallow copies — never mutates the caller's cart_items — and
    keeps the original `note` key too (belt and suspenders for any other
    reader).
    """
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            out.append(it)
            continue
        it2 = dict(it)
        note = (it2.get("note") or "").strip()
        if note and not it2.get("notes"):
            it2["notes"] = note
        out.append(it2)
    return out


def _order_payload(
    order_id: str, table_context: dict, restaurant_obj: dict | None, phone: str,
    items: list, total, base_order_id: str, sub_number: int, station: str,
    channel: str, notes: str, pending_table_validation: bool,
) -> dict:
    # org_id is the tenant key (FK into organizations). branch_id remains
    # the location_id (sede) for operational scoping — see CLAUDE.md
    # "Wave 2 (Org/Location)".
    org_id_val = (
        table_context.get("org_id")
        or (restaurant_obj.get("org_id") if restaurant_obj else None)
        or (restaurant_obj.get("id") if restaurant_obj else None)
    )
    branch_id_val = (
        table_context.get("branch_id")
        or table_context.get("location_id")
        or (restaurant_obj.get("location_id") if restaurant_obj else None)
    )
    return {
        "id": order_id,
        "table_id": table_context["id"],
        "table_name": table_context["name"],
        "phone": phone,
        "items": items,
        "notes": notes,
        "total": total,
        "status": "recibido",
        "base_order_id": base_order_id,
        "sub_number": sub_number,
        "station": station,
        "branch_id": branch_id_val,
        "org_id": org_id_val,
        "channel": channel,
        "pending_table_validation": pending_table_validation,
    }


async def save_table_order_round(
    *,
    is_new_group: bool,
    existing_base_order_id: str | None,
    table_context: dict,
    restaurant_obj: dict | None,
    phone: str,
    cart_items: list,
    cart_total,
    extra_notes: str,
    channel: str,
    pending_table_validation: bool,
    features: dict | None = None,
) -> dict:
    """Split `cart_items` by station, resolve base_order_id/sub_number, and
    save the table_order row(s) (one for kitchen, one more for bar when the
    restaurant has bar routing AND this round actually has bar items).

    `is_new_group=True` starts a fresh base_order_id (MESA-XXXXXX, sub 1) —
    used for the first order at a table, or when the caller explicitly wants
    a separate check (`separate_bill`). `is_new_group=False` requires
    `existing_base_order_id` and appends the next sub_number to it.

    Returns on success:
      {"success": True, "order_id": str, "base_order_id": str,
       "sub_number": int, "kitchen_items": list, "bar_items": list,
       "has_split": bool}
    Returns on bar-station save failure (kitchen order already saved; both
    are cancelled so neither reaches a screen half-committed):
      {"success": False, "error": "bar_order_failed"}
    """
    split = resolve_station_split(cart_items, features)
    kitchen_items = split["kitchen_items"]
    bar_items = split["bar_items"]
    has_split = split["has_split"]
    kitchen_station = split["kitchen_station"]

    if is_new_group:
        order_id = f"MESA-{uuid.uuid4().hex[:6].upper()}"
        base_order_id = order_id
        sub_number = 1
    else:
        if not existing_base_order_id:
            raise ValueError("existing_base_order_id is required when is_new_group=False")
        base_order_id = existing_base_order_id
        sub_number = await db.db_get_next_sub_number(base_order_id)
        order_id = f"{base_order_id}-{sub_number}"

    k_items = kitchen_items if has_split else cart_items
    k_total = station_total(k_items) if has_split else cart_total
    await db.db_save_table_order(
        _order_payload(
            order_id, table_context, restaurant_obj, phone, _with_ticket_notes(k_items), k_total,
            base_order_id, sub_number, kitchen_station, channel, extra_notes,
            pending_table_validation,
        )
    )

    if has_split and bar_items:
        bar_sub = await db.db_get_next_sub_number(base_order_id)
        bar_oid = f"{base_order_id}-{bar_sub}"
        try:
            await db.db_save_table_order(
                _order_payload(
                    bar_oid, table_context, restaurant_obj, phone, _with_ticket_notes(bar_items),
                    station_total(bar_items), base_order_id, bar_sub, "bar",
                    channel, extra_notes, pending_table_validation,
                )
            )
        except Exception:
            log.exception(
                "table_order_commit.bar_order_save_failed",
                order_id=bar_oid, base_order_id=base_order_id,
            )
            try:
                async with tenant_connection() as conn:
                    await conn.execute(
                        # 'cancelado' (Spanish) — the spelling every OTHER
                        # active-orders filter in the codebase checks for
                        # (db_get_table_orders_for_branch, db_get_active_table_order,
                        # db_get_order_ticket_data, ...). The pre-existing
                        # agent_salon.py code this was extracted from used
                        # the English 'cancelled' here, which those filters
                        # never matched — a cancelled-for-insufficient-stock
                        # or cancelled-for-bar-failure order kept showing up
                        # as "active" on the kitchen/mesero/caja screens.
                        "UPDATE table_orders SET status='cancelado' WHERE id=$1 OR base_order_id=$1",
                        order_id,
                    )
            except Exception:
                log.exception(
                    "table_order_commit.cancel_after_bar_failure_failed", order_id=order_id,
                )
            return {"success": False, "error": "bar_order_failed"}

    return {
        "success": True,
        "order_id": order_id,
        "base_order_id": base_order_id,
        "sub_number": sub_number,
        "kitchen_items": kitchen_items,
        "bar_items": bar_items,
        "has_split": has_split,
    }


async def deduct_inventory_or_cancel(bot_number: str, cart_items: list, saved_order_id: str | None,
                                     location_id: int | None = None) -> dict:
    """Attempt inventory deduction for a just-saved table order round.

    On InsufficientStockError (NO-ROMPER #13): cancels the saved order(s)
    (matching `id` OR `base_order_id` = saved_order_id) so they never reach
    the kitchen/bar screens, and returns a typed failure for the caller to
    surface to the customer. On any OTHER exception: logs and fails OPEN
    (returns success) — matches the pre-existing WhatsApp behaviour that an
    inventory-system hiccup must never silently block a legitimate order
    (Rule 8, "Nunca silencio al cliente" — the alternative, blocking the
    order, would be its own silent failure from the customer's POV).

    Returns {"success": True} or
    {"success": False, "error": "insufficient_stock", "sku": str,
     "message": str}.
    """
    try:
        # Stock is per sede — deduct from the sede this table belongs to.
        await db.db_deduct_inventory_for_order(bot_number, cart_items, location_id=location_id)
        return {"success": True}
    except InsufficientStockError as exc:
        log.warning(
            "table_order_commit.insufficient_stock",
            sku=exc.sku, requested=exc.requested, available=exc.available,
            bot_number=bot_number,
        )
        if saved_order_id:
            try:
                async with tenant_connection() as conn:
                    await conn.execute(
                        # 'cancelado' (Spanish) — the spelling every OTHER
                        # active-orders filter in the codebase checks for
                        # (db_get_table_orders_for_branch, db_get_active_table_order,
                        # db_get_order_ticket_data, ...). The pre-existing
                        # agent_salon.py code this was extracted from used
                        # the English 'cancelled' here, which those filters
                        # never matched — a cancelled-for-insufficient-stock
                        # or cancelled-for-bar-failure order kept showing up
                        # as "active" on the kitchen/mesero/caja screens.
                        "UPDATE table_orders SET status='cancelado' WHERE id=$1 OR base_order_id=$1",
                        saved_order_id,
                    )
            except Exception:
                log.exception(
                    "table_order_commit.cancel_after_stock_failure_failed",
                    order_id=saved_order_id,
                )
        return {
            "success": False,
            "error": "insufficient_stock",
            "sku": exc.sku,
            "message": f"Lo siento, '{exc.sku}' no está disponible en este momento.",
        }
    except Exception:
        log.exception("table_order_commit.inventory_deduction_failed", bot_number=bot_number)
        return {"success": True}
