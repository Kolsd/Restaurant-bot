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
               WHERE r.restaurant_id = $1 AND r.dish_name = $2""",
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
                   WHERE restaurant_id = $1
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
            """INSERT INTO menu_availability (dish_name, restaurant_id, available, updated_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (dish_name, restaurant_id)
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
        async with pool.acquire() as conn:
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
                if base_order_id:
                    await conn.execute(
                        """INSERT INTO orders
                               (id, phone, items, order_type, address, notes,
                                subtotal, delivery_fee, total, status, paid,
                                payment_url, bot_number, payment_method,
                                base_order_id, sub_number)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
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
                    )
                else:
                    await conn.execute(
                        """INSERT INTO orders
                               (id, phone, items, order_type, address, notes,
                                subtotal, delivery_fee, total, status, paid,
                                payment_url, bot_number, payment_method,
                                base_order_id, sub_number)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                           ON CONFLICT (id) DO UPDATE SET
                               items          = EXCLUDED.items,
                               subtotal       = EXCLUDED.subtotal,
                               total          = EXCLUDED.total,
                               status         = CASE
                                   WHEN orders.status IN (
                                       'en_preparacion','listo','en_camino','en_puerta','entregado'
                                   ) THEN orders.status
                                   ELSE EXCLUDED.status
                               END,
                               paid           = EXCLUDED.paid,
                               payment_url    = EXCLUDED.payment_url,
                               notes          = EXCLUDED.notes,
                               payment_method = EXCLUDED.payment_method""",
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
                payment_method, base_order_id, sub_number)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
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
        order.get("base_order_id"), order.get("sub_number", 1))

async def db_confirm_payment(order_id: str, transaction_id: str):
    async with _tenant_connection() as conn:
        row = await conn.fetchrow("""
            UPDATE orders SET paid=TRUE, status='confirmado', transaction_id=$2, paid_at=NOW()
            WHERE id=$1 RETURNING *
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
            rows = await conn.fetch(
                """SELECT o.* FROM orders o
                   JOIN restaurants r ON r.whatsapp_number = o.bot_number
                   WHERE o.order_type IN ('domicilio', 'recoger')
                     AND o.status = ANY($1)
                     AND r.id = $2
                   ORDER BY o.created_at ASC""",
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

async def db_update_order_status(order_id: str, new_status: str):
    """Actualiza el estado de un pedido y todas sus sub-órdenes con el mismo base_order_id."""
    async with _tenant_connection() as conn:
        await conn.execute("UPDATE orders SET status=$2 WHERE id=$1", order_id, new_status)
        # Cascade to sub-orders (base order id matches both base_order_id column and the passed id)
        await conn.execute(
            "UPDATE orders SET status=$2 WHERE base_order_id=$1 AND id != $1",
            order_id, new_status
        )
