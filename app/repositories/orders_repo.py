"""
Transactional order repository for delivery/pickup orders.

Wraps INSERT order → deduct inventory → delete cart in a single DB transaction
so partial writes cannot occur. Inventory deduction raises InsufficientStockError
instead of clamping to zero.

NOTE: table (mesa) orders are NOT handled here — they go through
db_save_table_order in agent.py which has its own multi-station logic.
"""

from __future__ import annotations

import json
from typing import Any, Union

from decimal import Decimal

from app.services.logging import get_logger
from app.services.money import to_decimal

log = get_logger(__name__)


# ── Custom exceptions ────────────────────────────────────────────────────────

class InsufficientStockError(Exception):
    """Raised when an inventory row does not have enough stock to fulfil an order."""

    def __init__(self, sku: str, requested: Union[Decimal, float, int], available: Union[Decimal, float, int]):
        self.sku = sku
        self.requested = requested
        self.available = available
        super().__init__(
            f"Stock insuficiente para '{sku}': solicitado={requested}, disponible={available}"
        )


class OrderCommitError(Exception):
    """Raised when the order transaction fails for reasons other than stock."""


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _deduct_inventory_in_tx(
    conn,
    restaurant_id: int,
    items: list[dict],
) -> None:
    """
    Deducts inventory inside an already-open transaction.

    Uses SELECT FOR UPDATE + WHERE current_stock >= deduct_amount RETURNING to
    ensure no negative stock ever reaches the DB. Raises InsufficientStockError
    on the first ingredient that fails the stock check.

    Supports both escandallo (dish_recipes) and legacy linked_dishes paths,
    mirroring db_deduct_inventory_for_order — but WITHOUT the max(0, stock) clamp.
    """
    for item in items:
        dish_name = item.get("name", "")
        qty = to_decimal(item.get("quantity", item.get("qty", 1)))
        if not dish_name or qty <= 0:
            continue

        # ── 1. Escandallo path ───────────────────────────────────────────────
        recipe_rows = await conn.fetch(
            """SELECT r.ingredient_id, r.quantity AS recipe_qty
               FROM dish_recipes r
               WHERE r.org_id = $1 AND r.dish_name = $2""",
            restaurant_id, dish_name,
        )

        if recipe_rows:
            ingredient_ids = [r["ingredient_id"] for r in recipe_rows]
            locked = await conn.fetch(
                """SELECT id, current_stock, min_stock, linked_dishes
                   FROM inventory
                   WHERE id = ANY($1::int[])
                   FOR UPDATE""",
                ingredient_ids,
            )
            locked_map = {r["id"]: r for r in locked}

            for rline in recipe_rows:
                ing_id = rline["ingredient_id"]
                deduct = to_decimal(rline["recipe_qty"]) * qty
                inv = locked_map.get(ing_id)
                if not inv:
                    continue

                # Atomic check-and-update: only succeeds if stock is sufficient
                updated = await conn.fetchrow(
                    """UPDATE inventory
                       SET current_stock = current_stock - $1,
                           updated_at    = NOW()
                       WHERE id = $2
                         AND current_stock >= $1
                       RETURNING current_stock""",
                    deduct, ing_id,
                )
                if updated is None:
                    available = to_decimal(inv["current_stock"])
                    raise InsufficientStockError(
                        sku=f"{dish_name} (ingrediente id={ing_id})",
                        requested=deduct,
                        available=available,
                    )

                new_stock = to_decimal(updated["current_stock"])
                await conn.execute(
                    """INSERT INTO inventory_history
                       (inventory_id, quantity_delta, stock_after, reason)
                       VALUES ($1, $2, $3, 'orden_confirmada')""",
                    ing_id, -deduct, new_stock,
                )

                min_stock = to_decimal(inv["min_stock"] or 0)
                # Sync dish_recipes-based availability (Fase 5c)
                from app.repositories.inventory_repo import _sync_ingredient_dishes_conn
                await _sync_ingredient_dishes_conn(conn, ing_id, float(new_stock), float(min_stock), restaurant_id)
                # Also sync legacy linked_dishes on the same ingredient
                if new_stock <= min_stock:
                    dishes = inv["linked_dishes"]
                    if isinstance(dishes, str):
                        dishes = json.loads(dishes)
                    if dishes:
                        await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)

        else:
            # ── 2. Legacy linked_dishes path ─────────────────────────────────
            rows = await conn.fetch(
                """SELECT id, current_stock, linked_dishes, min_stock
                   FROM inventory
                   WHERE org_id = $1
                     AND linked_dishes @> $2::jsonb
                   FOR UPDATE""",
                restaurant_id, json.dumps([dish_name]),
            )
            for row in rows:
                available = to_decimal(row["current_stock"])
                updated = await conn.fetchrow(
                    """UPDATE inventory
                       SET current_stock = current_stock - $1,
                           updated_at    = NOW()
                       WHERE id = $2
                         AND current_stock >= $1
                       RETURNING current_stock""",
                    qty, row["id"],
                )
                if updated is None:
                    raise InsufficientStockError(
                        sku=dish_name,
                        requested=qty,
                        available=available,
                    )

                new_stock = to_decimal(updated["current_stock"])
                await conn.execute(
                    """INSERT INTO inventory_history
                       (inventory_id, quantity_delta, stock_after, reason)
                       VALUES ($1, $2, $3, 'orden_confirmada')""",
                    row["id"], -qty, new_stock,
                )

                dishes = row["linked_dishes"]
                if isinstance(dishes, str):
                    dishes = json.loads(dishes)
                min_stock = to_decimal(row["min_stock"] or 0)
                if new_stock <= min_stock and dishes:
                    await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)


async def _sync_dish_availability_conn(
    conn, dish_names: list[str], available: bool, restaurant_id: int
) -> None:
    """Mirror of database._sync_dish_availability_conn — used inside the transaction."""
    for name in dish_names:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, org_id, available, updated_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (dish_name, org_id)
               DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
            name, restaurant_id, available,
        )


# ── Public API ───────────────────────────────────────────────────────────────

async def commit_order_transaction(
    pool,
    *,
    restaurant_id: int,
    conversation_id: str,
    cart: dict[str, Any],
    order_payload: dict[str, Any],
    channel: str | None = None,
    location_id: int | None = None,
) -> None:
    """
    Atomically commits a delivery/pickup order:
      1. INSERT into `orders` (upsert — same SQL as db_save_order)
      2. Deduct inventory for each item (raises InsufficientStockError on shortage)
      3. DELETE cart row

    Args:
        pool:           asyncpg pool obtained from db.get_pool().
        restaurant_id:  Numeric restaurant ID (used for inventory lookup).
        conversation_id: Phone / conversation identifier (used for cart delete).
        cart:           Full cart dict (contains 'items' and 'bot_number').
        order_payload:  Order dict — same shape expected by db_save_order.
        channel:        Optional attribution channel (whatsapp_bot, pos, qr_pickup, web, manual).
                        If provided, stored on the orders row. Legacy rows leave this NULL.
        location_id:    Optional location_id (sede) resolved by GPS routing / branch selection.
                        Stored for dashboard filtering by branch. NULL means location not yet
                        resolved (pre-Wave-2 rows and orders from single-location tenants
                        with no explicit branch routing also receive NULL).

    Raises:
        InsufficientStockError: one ingredient is out of stock; transaction rolled back.
        OrderCommitError:       any other DB failure; transaction rolled back.
    """
    bot_number = order_payload.get("bot_number", "")
    order_id = order_payload["id"]
    items = order_payload.get("items", [])

    # Safety net: coerce financial fields to Decimal so callers can't pass raw floats
    # that would silently lose precision through the asyncpg NUMERIC binding.
    for _field in ("subtotal", "delivery_fee", "total"):
        if _field in order_payload:
            order_payload[_field] = to_decimal(order_payload[_field])

    # structlog exposes .bind(); the stdlib adapter does not — use module logger directly
    _log = (
        log.bind(restaurant_id=restaurant_id, conversation_id=conversation_id, order_id=order_id)
        if hasattr(log, "bind")
        else log
    )

    try:
        async with _tenant_connection() as conn:
            async with conn.transaction():
                # 1a. For sub-orders: recompute sub_number atomically inside the
                #     transaction to prevent two concurrent workers from calculating
                #     the same sub_number and silently overwriting each other.
                base_order_id = order_payload.get("base_order_id")
                if base_order_id:
                    # Lock the base order row so concurrent sub-order inserts serialize here
                    await conn.fetchrow(
                        "SELECT id FROM orders WHERE id=$1 FOR UPDATE",
                        base_order_id,
                    )
                    max_sub = await conn.fetchval(
                        "SELECT COALESCE(MAX(sub_number), 1) FROM orders WHERE base_order_id=$1 OR id=$1",
                        base_order_id,
                    )
                    sub_number = max_sub + 1
                    order_payload["sub_number"] = sub_number
                    order_payload["id"] = f"{base_order_id}-{sub_number}"
                    order_id = order_payload["id"]

                # 1b. Insert the order row — no ON CONFLICT DO UPDATE for sub-orders;
                #     a genuine id collision (which should not happen after the atomic
                #     sub_number computation above) raises a unique-violation error that
                #     the outer except block converts to OrderCommitError.
                # Parse scheduled_pickup_at: accept ISO string or None
                _spa_raw = order_payload.get("scheduled_pickup_at")
                _scheduled_pickup_at = None
                if _spa_raw:
                    try:
                        from datetime import datetime as _dt  # noqa: PLC0415
                        _scheduled_pickup_at = _dt.fromisoformat(str(_spa_raw))
                    except (ValueError, TypeError):
                        _log.warning(
                            "order_commit.invalid_scheduled_pickup_at",
                            raw=str(_spa_raw)[:64],
                        )

                if base_order_id:
                    await conn.execute(
                        """INSERT INTO orders
                               (id, phone, items, order_type, address, notes,
                                subtotal, delivery_fee, total, status, paid,
                                payment_url, bot_number, payment_method,
                                base_order_id, sub_number, org_id, channel, location_id,
                                scheduled_pickup_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)""",
                        order_payload["id"],
                        order_payload["phone"],
                        json.dumps(order_payload["items"]),
                        order_payload["order_type"],
                        order_payload.get("address", ""),
                        order_payload.get("notes", ""),
                        order_payload["subtotal"],
                        order_payload["delivery_fee"],
                        order_payload["total"],
                        order_payload["status"],
                        order_payload["paid"],
                        order_payload.get("payment_url", ""),
                        bot_number,
                        order_payload.get("payment_method", ""),
                        base_order_id,
                        order_payload["sub_number"],
                        restaurant_id,
                        channel,
                        location_id,
                        _scheduled_pickup_at,
                    )
                else:
                    await conn.execute(
                        """INSERT INTO orders
                               (id, phone, items, order_type, address, notes,
                                subtotal, delivery_fee, total, status, paid,
                                payment_url, bot_number, payment_method,
                                base_order_id, sub_number, org_id, channel, location_id,
                                scheduled_pickup_at)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                           ON CONFLICT (id) DO UPDATE SET
                               items               = EXCLUDED.items,
                               subtotal            = EXCLUDED.subtotal,
                               total               = EXCLUDED.total,
                               status              = CASE
                                   WHEN orders.status IN (
                                       'en_preparacion','listo','en_camino','en_puerta','entregado'
                                   ) THEN orders.status
                                   ELSE EXCLUDED.status
                               END,
                               paid                = EXCLUDED.paid,
                               payment_url         = EXCLUDED.payment_url,
                               notes               = EXCLUDED.notes,
                               payment_method      = EXCLUDED.payment_method,
                               location_id         = COALESCE(orders.location_id, EXCLUDED.location_id),
                               scheduled_pickup_at = COALESCE(orders.scheduled_pickup_at, EXCLUDED.scheduled_pickup_at)""",
                        order_payload["id"],
                        order_payload["phone"],
                        json.dumps(order_payload["items"]),
                        order_payload["order_type"],
                        order_payload.get("address", ""),
                        order_payload.get("notes", ""),
                        order_payload["subtotal"],
                        order_payload["delivery_fee"],
                        order_payload["total"],
                        order_payload["status"],
                        order_payload["paid"],
                        order_payload.get("payment_url", ""),
                        bot_number,
                        order_payload.get("payment_method", ""),
                        None,
                        order_payload.get("sub_number", 1),
                        restaurant_id,
                        channel,
                        location_id,
                        _scheduled_pickup_at,
                    )

                # 2. Deduct inventory (raises InsufficientStockError on shortage)
                if items:
                    await _deduct_inventory_in_tx(conn, restaurant_id, items)

                # 3. Delete the cart row — phone is the cart PK column
                await conn.execute(
                    "DELETE FROM carts WHERE phone = $1 AND bot_number = $2",
                    conversation_id, bot_number,
                )

    except InsufficientStockError:
        # Let callers handle this with a user-friendly message
        raise
    except Exception as exc:
        _log.exception("order_commit_failed", order_id=order_payload.get("id"), error=str(exc))
        raise OrderCommitError(f"Order commit failed for order '{order_payload.get('id')}': {exc}") from exc


# ── Lazy wrappers (break circular import with database.py) ────────────────────

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()

def _serialize(d):
    from app.services.database import _serialize
    return _serialize(d)

def _to_date(s):
    from app.services.database import _to_date
    return _to_date(s)


# ── ORDENES DELIVERY ─────────────────────────────────────────────────────────

async def db_save_order(order: dict):
    async with _tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO orders (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, payment_url, bot_number,
                payment_method, base_order_id, sub_number, org_id, channel)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items,
                subtotal=EXCLUDED.subtotal,
                total=EXCLUDED.total,
                status=CASE
                    WHEN orders.status IN ('en_preparacion','listo','en_camino','en_puerta','entregado')
                    THEN orders.status
                    ELSE EXCLUDED.status
                END,
                paid=EXCLUDED.paid,
                payment_url=EXCLUDED.payment_url,
                notes=EXCLUDED.notes,
                payment_method=EXCLUDED.payment_method
        """,
        order["id"], order["phone"], json.dumps(order["items"]),
        order["order_type"], order.get("address", ""), order.get("notes", ""),
        order["subtotal"], order["delivery_fee"], order["total"],
        order["status"], order["paid"], order.get("payment_url", ""),
        order.get("bot_number", ""), order.get("payment_method", ""),
        order.get("base_order_id"), order.get("sub_number", 1),
        order.get("restaurant_id") or order.get("org_id"),
        order.get("channel"))

async def db_confirm_payment(order_id: str, transaction_id: str):
    """
    Idempotent payment confirmation.

    Adds guards so that:
      - already-paid orders are not re-confirmed (idempotent Wompi retries)
      - cancelled / delivered orders cannot be flipped back to 'confirmado'

    Returns the updated row dict, or None if no eligible row was found
    (already paid, cancelled, or delivered).  The caller should treat None
    as a no-op success (return 200 to Wompi, log wompi.webhook.noop).
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow("""
            UPDATE orders
               SET paid=TRUE, status='confirmado', transaction_id=$2, paid_at=NOW()
             WHERE id=$1
               AND paid = FALSE
               AND status NOT IN ('cancelado', 'entregado')
            RETURNING *
        """, order_id, transaction_id)
        return _serialize(dict(row)) if row else None

async def db_get_orders_range(date_from: str, date_to: str, bot_number: str = None):
    from datetime import timedelta
    d_from = _to_date(date_from)
    d_to_inclusive = _to_date(date_to) + timedelta(days=1)
    async with _tenant_connection() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2 AND bot_number=$3 ORDER BY created_at DESC", d_from, d_to_inclusive, bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2 ORDER BY created_at DESC", d_from, d_to_inclusive)
        return [_serialize(dict(r)) for r in rows]

async def db_get_order(order_id: str):
    async with _tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        return _serialize(dict(row)) if row else None

async def db_get_all_orders(bot_number: str = None):
    async with _tenant_connection() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM orders WHERE bot_number=$1 ORDER BY created_at DESC", bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]

async def db_get_delivery_orders(status_list: list, restaurant_id: int | None = None):
    """Obtiene los pedidos de domicilio filtrados por una lista de estados.
    Si restaurant_id se provee, filtra exactamente por ese restaurante/sucursal (r.id = restaurant_id).
    Cada caja ve únicamente sus propios pedidos — sin herencia de sucursales."""
    async with _tenant_connection() as conn:
        if restaurant_id is not None:
            # Filter directly on orders.org_id — no JOIN needed.
            # The previous JOIN restaurants → locations multiplied each order row
            # by the number of locations sharing the same whatsapp_number.
            rows = await conn.fetch(
                """SELECT * FROM orders
                   WHERE order_type IN ('domicilio', 'recoger')
                     AND status = ANY($1)
                     AND org_id = $2
                   ORDER BY created_at ASC""",
                status_list, restaurant_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM orders WHERE order_type IN ('domicilio', 'recoger') AND status = ANY($1) ORDER BY created_at ASC",
                status_list
            )
        return [_serialize(dict(r)) for r in rows]

async def db_update_pending_order_payment_method(phone: str, bot_number: str, payment_method: str):
    """Updates payment_method on the most recent pending delivery/pickup order for a phone+bot."""
    async with _tenant_connection() as conn:
        await conn.execute(
            """UPDATE orders SET payment_method=$3
               WHERE id = (
                 SELECT id FROM orders
                 WHERE phone=$1 AND bot_number=$2
                   AND order_type IN ('domicilio','recoger')
                   AND paid=false
                   AND status NOT IN ('cancelado','entregado')
                 ORDER BY created_at DESC LIMIT 1
               )""",
            phone, bot_number, payment_method
        )


async def db_attach_order_proof(phone: str, bot_number: str, media_url: str) -> str | None:
    """
    Adjunta URL de comprobante al pedido delivery/pickup más reciente NO PAGADO
    de phone+bot_number. Retorna order_id si se vinculó, None si no hubo match.

    Pareja del table_orders db_attach_proof, pero para órdenes externas. Solo
    toca órdenes paid=false y no-terminales para evitar pisar comprobantes ya
    validados o adjuntar a órdenes canceladas.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE orders SET proof_url=$3
               WHERE id = (
                 SELECT id FROM orders
                 WHERE phone=$1 AND bot_number=$2
                   AND order_type IN ('domicilio','recoger')
                   AND paid=false
                   AND status NOT IN ('cancelado','entregado')
                 ORDER BY created_at DESC LIMIT 1
               )
               RETURNING id""",
            phone, bot_number, media_url,
        )
        return row["id"] if row else None

async def db_update_order_status(order_id: str, new_status: str) -> dict | None:
    """
    Actualiza el estado de un pedido y todas sus sub-órdenes con el mismo base_order_id.

    Returns the updated main-order row dict, or None if the status was already
    the same value (or the order does not exist) — so the caller can detect
    race conditions and return 409 Conflict instead of silently accepting a
    no-op update.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE orders SET status=$2 WHERE id=$1 AND status != $2 RETURNING *",
            order_id, new_status,
        )
        if row is None:
            return None
        # Cascade to sub-orders (base order id matches both base_order_id column and the passed id)
        await conn.execute(
            "UPDATE orders SET status=$2 WHERE base_order_id=$1 AND id != $1",
            order_id, new_status,
        )
        return _serialize(dict(row))


# ── Wompi webhook idempotency (GLOBAL table) ──────────────────────────────────
#
# processed_wompi_events has no restaurant_id column and is explicitly GLOBAL
# — the event_id is resolved before we know which tenant owns the order.
# Pattern mirrors sessions_repo / inbox_repo: uses get_pool() directly, NOT
# tenant_connection().  Do NOT add tenant_scope here.

async def record_wompi_event(
    event_id: str,
    *,
    transaction_id: str | None,
    order_id: int | None,
    status: str,
) -> bool:
    """Record a processed Wompi event and signal whether it is new.

    Inserts a row for *event_id* using INSERT … ON CONFLICT (event_id) DO NOTHING.
    Returns True if this is the first time we have seen this event_id (the
    caller should continue processing), or False if the row already existed
    (a replay — the caller should return 200 immediately without side effects).

    Calling this function 100 times with the same event_id is safe: after the
    first insert every subsequent call is a no-op that returns False.

    GLOBAL table — does NOT require tenant_scope.
    """
    def _get_pool():
        from app.services.database import get_pool  # noqa: PLC0415
        return get_pool()

    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO processed_wompi_events
                (event_id, transaction_id, order_id, status)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event_id,
            transaction_id,
            order_id,
            status,
        )
    # asyncpg returns "INSERT 0 N" — N=1 means row was inserted (first time).
    affected = int(result.split()[-1]) if result else 0
    return affected == 1


# ── Customer self-cancellation ───────────────────────────────────────────────

async def db_cancel_pending_order(
    phone: str,
    bot_number: str,
    reason: str | None = None,
) -> dict | None:
    """
    Cancel the most recent active delivery/pickup order for a customer IF its
    status is still 'pendiente' (before kitchen confirmation).

    Returns a dict with keys:
        "cancelled": True  — order was found and cancelled
        "too_late":  True  — order exists but is past 'pendiente'; cannot cancel
        "not_found": True  — no active order found for this customer

    DB side effect: sets status='cancelado', cancelled_at=NOW(), cancelled_reason=$reason.
    Wraps inside tenant_connection() — the caller must have an active tenant_scope.
    """
    async with _tenant_connection() as conn:
        # Find the most recent non-terminal order for this customer
        row = await conn.fetchrow(
            """
            SELECT id, status
            FROM orders
            WHERE phone      = $1
              AND bot_number = $2
              AND status NOT IN ('cancelado', 'entregado')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            phone, bot_number,
        )

    if row is None:
        return {"not_found": True}

    if row["status"] != "pendiente":
        return {"too_late": True, "status": row["status"]}

    # Status is 'pendiente' — cancel it
    async with _tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE orders
               SET status           = 'cancelado',
                   cancelled_at     = NOW(),
                   cancelled_reason = $2
             WHERE id = $1
            """,
            row["id"], reason or None,
        )

    return {"cancelled": True, "order_id": row["id"]}


# ── Caja customer helpers ────────────────────────────────────────────────────

async def db_get_recent_orders_by_phone(
    org_id: int,
    phone: str,
    limit: int = 5,
) -> list[dict]:
    """Return the most recent delivery/pickup orders for a phone number.

    Runs under the already-active tenant_scope set by the call site.
    Returns a list of dicts:
        {"id", "total": float, "created_at": str | None, "items_summary": str, "source": "delivery"}
    """
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415

    results: list[dict] = []
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, total, created_at, items
               FROM orders
               WHERE phone = $1
                 AND org_id = $2
               ORDER BY created_at DESC
               LIMIT $3""",
            phone, org_id, limit,
        )
    for r in rows:
        items_raw = r["items"]
        if isinstance(items_raw, str):
            try:
                items_raw = json.loads(items_raw)
            except Exception:
                items_raw = []
        items_list = items_raw if isinstance(items_raw, list) else []
        summary = " · ".join(i.get("name", "item") for i in items_list[:3])
        if len(items_list) > 3:
            summary += f" +{len(items_list) - 3}"
        results.append({
            "id": f"#{str(r['id'])[-6:].upper()}",
            "total": float(to_decimal(r["total"])),  # JSON boundary
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "items_summary": summary,
            "source": "delivery",
        })
    return results


# ── Delivery ETA (estimated_minutes) ─────────────────────────────────────────
#
# Pair with migration 0064. Three primitives drive the flow:
#   set_eta            → admin enters ETA via /api/delivery/orders/{id}/eta
#   needing_communication → scheduler scans pending ones every 3 min
#   mark_communicated  → scheduler flips the flag after WhatsApp sends
#
# All three are tenant-scoped (rely on RLS). The scheduler enumerates orgs
# under bypass and enters tenant_scope per-org before calling the second one.

async def db_set_order_eta(order_id: str, estimated_minutes: int) -> dict | None:
    """Set the ETA for a delivery/pickup order.

    Resets eta_communicated to FALSE so the scheduler picks the row up on
    its next sweep, even if a previous ETA was already communicated. This
    lets admins update the ETA mid-flight ("we're running 10 min later").

    Returns the updated row dict, or None if the order does not exist.

    # Requires active tenant_scope().
    """
    if estimated_minutes is None:
        raise ValueError("estimated_minutes must not be None")
    minutes = int(estimated_minutes)
    if minutes < 1 or minutes > 180:
        raise ValueError(f"estimated_minutes must be 1..180, got {minutes}")

    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE orders
               SET estimated_minutes = $2,
                   eta_communicated  = FALSE
             WHERE id = $1
            RETURNING id, estimated_minutes, eta_communicated, status, paid,
                      phone, bot_number, order_type
            """,
            order_id, minutes,
        )
        return dict(row) if row else None


async def db_get_orders_needing_eta_communication() -> list[dict]:
    """Return paid orders with an ETA that has not been WhatsApp'd yet.

    Filtered to active statuses (confirmado, en_preparacion). Orders that
    already shipped (en_camino, en_puerta, entregado) are excluded — their
    notification path is handled by send_delivery_notification on status
    transitions, not by the ETA scheduler.

    # Requires active tenant_scope() (per-org wrap by the scheduler).
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, phone, bot_number, estimated_minutes, status, order_type
            FROM orders
            WHERE paid              = TRUE
              AND status            IN ('confirmado', 'en_preparacion')
              AND estimated_minutes IS NOT NULL
              AND eta_communicated  = FALSE
            ORDER BY created_at ASC
            """,
        )
        return [dict(r) for r in rows]


async def db_mark_eta_communicated(order_id: str) -> bool:
    """Atomically flip eta_communicated=TRUE — single-winner.

    Returns True on the FIRST mark only. A second worker calling this on
    the same order returns False, so we never double-WhatsApp the customer
    even if two scheduler ticks race.

    # Requires active tenant_scope().
    """
    async with _tenant_connection() as conn:
        result = await conn.execute(
            """
            UPDATE orders
               SET eta_communicated = TRUE
             WHERE id = $1
               AND eta_communicated = FALSE
            """,
            order_id,
        )
        return result == "UPDATE 1"
