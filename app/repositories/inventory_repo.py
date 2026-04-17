"""
Inventory repository — Fase 6 extraction from app.services.database.

Covers the inventory aggregate:
  - inventory items (CRUD, stock adjustment, alerts, history)
  - dish_recipes / escandallos (upsert, get, list, delete, food costs)
  - stock deduction for orders (legacy path used by agent.py)
  - menu_availability sync helpers (_sync_dish_availability_conn, _sync_dish_availability)
  - DDL init functions (kept for reference; schema is now managed by Alembic)

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.
"""

from __future__ import annotations

import json

from app.repositories.orders_repo import InsufficientStockError


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()

def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── Internal helpers ─────────────────────────────────────────────────────────

async def _sync_dish_availability_conn(conn, dish_names: list, available: bool, restaurant_id: int):
    """Activa o desactiva platos usando una conexión existente (dentro de transacción)."""
    for name in dish_names:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, restaurant_id, available, updated_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (dish_name, restaurant_id)
               DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
            name, restaurant_id, available
        )


async def _sync_dish_availability(dish_names: list, available: bool, restaurant_id: int):
    """Activa o desactiva platos en menu_availability según el stock."""
    if not dish_names:
        return
    async with _tenant_connection() as conn:
        await _sync_dish_availability_conn(conn, dish_names, available, restaurant_id)


async def _sync_ingredient_dishes_conn(
    conn, ingredient_id: int, new_stock: float, min_stock: float, restaurant_id: int
) -> None:
    """
    Cuando un ingrediente baja a <= min_stock, busca TODOS los platos que lo usan
    vía dish_recipes y los marca unavailable en menu_availability.
    También sincroniza linked_dishes legacy del mismo ingrediente.

    Llamar dentro de una transacción abierta (conn ya tiene lock sobre el ingrediente).
    No falla silenciosamente: si hay error en el INSERT de menu_availability,
    se propagará al caller para que la transacción haga rollback.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    if new_stock > min_stock:
        # Stock repuesto — re-evaluar platos afectados para marcarlos available
        # Solo si TODOS sus otros ingredientes también tienen stock > min_stock.
        await _recheck_dishes_for_ingredient_conn(conn, ingredient_id, restaurant_id)
        return

    # Stock agotado o en mínimo — marcar platos como no disponibles
    recipe_rows = await conn.fetch(
        "SELECT dish_name FROM dish_recipes WHERE ingredient_id = $1 AND restaurant_id = $2",
        ingredient_id, restaurant_id,
    )
    dish_names = [r["dish_name"] for r in recipe_rows]

    if dish_names:
        log.info(
            "inventory.auto_hide",
            ingredient_id=ingredient_id,
            dishes=dish_names,
            new_stock=new_stock,
            restaurant_id=restaurant_id,
        )
        await _sync_dish_availability_conn(conn, dish_names, False, restaurant_id)


async def _recheck_dishes_for_ingredient_conn(
    conn, ingredient_id: int, restaurant_id: int
) -> None:
    """
    Tras reponer stock, re-evalúa cada plato que usa este ingrediente.
    Un plato vuelve a estar disponible solo si TODOS sus ingredientes tienen
    current_stock > min_stock.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    recipe_rows = await conn.fetch(
        "SELECT dish_name FROM dish_recipes WHERE ingredient_id = $1 AND restaurant_id = $2",
        ingredient_id, restaurant_id,
    )
    for row in recipe_rows:
        dish_name = row["dish_name"]
        # Contar ingredientes totales vs ingredientes con stock OK
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM dish_recipes WHERE dish_name = $1 AND restaurant_id = $2",
            dish_name, restaurant_id,
        )
        ok = await conn.fetchval(
            """SELECT COUNT(*)
               FROM dish_recipes r
               JOIN inventory i ON i.id = r.ingredient_id
               WHERE r.dish_name = $1 AND r.restaurant_id = $2
                 AND i.current_stock > i.min_stock""",
            dish_name, restaurant_id,
        )
        available = (ok == total)
        log.info(
            "inventory.recheck_dish",
            dish=dish_name,
            available=available,
            ok_ingredients=ok,
            total_ingredients=total,
            restaurant_id=restaurant_id,
        )
        await _sync_dish_availability_conn(conn, [dish_name], available, restaurant_id)


# ── DDL init (legacy stubs — schema is fully managed by Alembic) ─────────────
# Tables: nps_responses, inventory, inventory_history, nps_waiting → 0001_initial_schema.py
# Table:  dish_recipes → 0001_initial_schema.py
# Indexes: idx_table_orders_base, idx_table_orders_station → 0001_initial_schema.py
# Index:   idx_rest_tables_lookup → 0020_missing_runtime_tables.py
# Columns: conversations.created_at, restaurants.google_maps_url → 0001_initial_schema.py

async def db_init_nps_inventory():
    """No-op: schema handled by Alembic (run `alembic upgrade head` before deploying)."""
    pass


async def db_init_dish_recipes():
    """No-op: schema handled by Alembic (run `alembic upgrade head` before deploying)."""
    pass


# ── Inventory CRUD ────────────────────────────────────────────────────────────

async def db_get_inventory(restaurant_id: int) -> list:
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            "SELECT * FROM inventory WHERE restaurant_id = $1 ORDER BY name ASC",
            restaurant_id
        )
    return [_serialize(dict(r)) for r in rows]


async def db_create_inventory_item(restaurant_id: int, name: str, unit: str,
                                    current_stock: float, min_stock: float,
                                    linked_dishes: list, cost_per_unit: float = 0) -> dict:
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO inventory
               (restaurant_id, name, unit, current_stock, min_stock, linked_dishes, cost_per_unit)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
               RETURNING *""",
            restaurant_id, name, unit, current_stock, min_stock,
            json.dumps(linked_dishes), cost_per_unit
        )
        item = _serialize(dict(row))
        # Si el stock es 0, desactivar platos vinculados
        if current_stock <= 0:
            await _sync_dish_availability(linked_dishes, False, restaurant_id)
        return item


async def db_update_inventory_item(item_id: int, fields: dict) -> dict | None:
    async with _tenant_connection() as conn:
        # FOR UPDATE acquires a row-level lock for the duration of the
        # tenant_connection() transaction, preventing a concurrent admin edit
        # from producing a lost-update (TOCTOU: read-then-write race).
        existing = await conn.fetchrow(
            "SELECT * FROM inventory WHERE id = $1 FOR UPDATE", item_id
        )
        if not existing:
            return None

        # Construimos el SET dinámico
        allowed = {"name", "unit", "current_stock", "min_stock", "linked_dishes", "cost_per_unit"}
        set_parts = []
        values = []
        idx = 1
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "linked_dishes":
                set_parts.append(f"{k} = ${idx}::jsonb")
                values.append(json.dumps(v))
            else:
                set_parts.append(f"{k} = ${idx}")
                values.append(v)
            idx += 1

        if not set_parts:
            return _serialize(dict(existing))

        set_parts.append(f"updated_at = NOW()")
        values.append(item_id)
        query = f"UPDATE inventory SET {', '.join(set_parts)} WHERE id = ${idx} RETURNING *"
        row = await conn.fetchrow(query, *values)
        item = _serialize(dict(row))

        new_stock = fields.get("current_stock", existing["current_stock"])
        dishes    = fields.get("linked_dishes", existing["linked_dishes"])
        if isinstance(dishes, str):
            dishes = json.loads(dishes)

        restaurant_id = existing["restaurant_id"]

        await _sync_dish_availability(dishes, new_stock > 0, restaurant_id)
        return item


async def db_delete_inventory_item(item_id: int):
    async with _tenant_connection() as conn:
        await conn.execute("DELETE FROM inventory WHERE id = $1", item_id)


async def db_adjust_inventory_stock(item_id: int, quantity_delta: float,
                                     reason: str, restaurant_id: int) -> dict | None:
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE inventory
               SET current_stock = GREATEST(0, current_stock + $1),
                   updated_at = NOW()
               WHERE id = $2 AND restaurant_id = $3
               RETURNING *""",
            quantity_delta, item_id, restaurant_id
        )
        if not row:
            return None
        item = _serialize(dict(row))

        # Registrar en historial
        await conn.execute(
            """INSERT INTO inventory_history (inventory_id, quantity_delta, stock_after, reason)
               VALUES ($1, $2, $3, $4)""",
            item_id, quantity_delta, item["current_stock"], reason
        )

        # Sincronizar disponibilidad de platos vinculados
        dishes = item.get("linked_dishes", [])
        if isinstance(dishes, str):
            dishes = json.loads(dishes)
        await _sync_dish_availability(dishes, float(item["current_stock"]) > 0, restaurant_id)
        return item


async def db_get_inventory_item(item_id: int) -> dict | None:
    """Obtiene un item de inventario por ID. Tenant-scoped via RLS."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, restaurant_id, name, unit, current_stock, min_stock, "
            "linked_dishes, cost_per_unit FROM inventory WHERE id = $1",
            item_id,
        )
    return _serialize(dict(row)) if row else None


async def db_get_inventory_history(item_id: int) -> list:
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory_history
               WHERE inventory_id = $1
               ORDER BY created_at DESC
               LIMIT 100""",
            item_id
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_inventory_alerts(restaurant_id: int) -> list:
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory
               WHERE restaurant_id = $1
                 AND current_stock <= min_stock
               ORDER BY current_stock ASC""",
            restaurant_id
        )
    return [_serialize(dict(r)) for r in rows]


async def db_deduct_inventory_for_order(bot_number: str, items: list):
    """
    Descuenta stock por cada plato pedido, con soporte para escandallos (dish_recipes).
    items = [{"name": "Hamburguesa Clásica", "quantity": 2}, ...]
    Usa SELECT FOR UPDATE dentro de una transacción para evitar race conditions
    con los 4 workers de Railway.
    Si no hay receta definida, cae al comportamiento legacy de linked_dishes.

    NOTA: Para órdenes de domicilio/recoger, usar commit_order_transaction en
    app.repositories.orders_repo, que envuelve esto junto con db_save_order y
    la limpieza del carrito en una sola transacción.

    Raises:
        InsufficientStockError: si el stock de un ingrediente es insuficiente.
    """
    # Lazy import to avoid circular dependency:
    # database.py imports inventory_repo (via re-export), inventory_repo imports
    # db_get_restaurant_by_phone from database — use lazy import to break cycle.
    from app.services.database import db_get_restaurant_by_phone

    restaurant = await db_get_restaurant_by_phone(bot_number)
    if not restaurant:
        return

    restaurant_id = restaurant["id"]

    # tenant_connection() already wraps in a transaction — no nested tx needed
    async with _tenant_connection() as conn:
            for item in items:
                dish_name = item.get("name", "")
                qty       = float(item.get("quantity", item.get("qty", 1)))
                if not dish_name or qty <= 0:
                    continue

                # ── 1. Intentar receta (escandallo) ──────────────────────
                recipe_rows = await conn.fetch(
                    """SELECT r.ingredient_id, r.quantity AS recipe_qty
                       FROM dish_recipes r
                       WHERE r.restaurant_id = $1 AND r.dish_name = $2""",
                    restaurant_id, dish_name
                )

                if recipe_rows:
                    # Bloquear las filas de ingredientes antes de modificar
                    ingredient_ids = [r["ingredient_id"] for r in recipe_rows]
                    locked = await conn.fetch(
                        """SELECT id, current_stock, min_stock, linked_dishes
                           FROM inventory
                           WHERE id = ANY($1::int[])
                           FOR UPDATE""",
                        ingredient_ids
                    )
                    locked_map = {r["id"]: r for r in locked}

                    for rline in recipe_rows:
                        ing_id    = rline["ingredient_id"]
                        deduct    = float(rline["recipe_qty"]) * qty
                        inv       = locked_map.get(ing_id)
                        if not inv:
                            continue
                        updated = await conn.fetchrow(
                            """UPDATE inventory
                               SET current_stock = current_stock - $1,
                                   updated_at    = NOW()
                               WHERE id = $2
                                 AND current_stock >= $1
                               RETURNING current_stock""",
                            deduct, ing_id
                        )
                        if updated is None:
                            available = float(inv["current_stock"])
                            raise InsufficientStockError(
                                sku=f"{dish_name} (ingrediente id={ing_id})",
                                requested=deduct,
                                available=available,
                            )
                        new_stock = float(updated["current_stock"])
                        await conn.execute(
                            """INSERT INTO inventory_history
                               (inventory_id, quantity_delta, stock_after, reason)
                               VALUES ($1, $2, $3, 'orden_confirmada')""",
                            ing_id, -deduct, new_stock
                        )
                        min_stock_val = float(inv["min_stock"] or 0)
                        # Sync dish_recipes-based availability (Fase 5c)
                        await _sync_ingredient_dishes_conn(conn, ing_id, new_stock, min_stock_val, restaurant_id)
                        # Also sync legacy linked_dishes on the same ingredient
                        if new_stock <= min_stock_val:
                            dishes = inv["linked_dishes"]
                            if isinstance(dishes, str):
                                dishes = json.loads(dishes)
                            if dishes:
                                await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)

                else:
                    # ── 2. Fallback legacy: linked_dishes ────────────────
                    rows = await conn.fetch(
                        """SELECT id, current_stock, linked_dishes, min_stock
                           FROM inventory
                           WHERE restaurant_id = $1
                             AND linked_dishes @> $2::jsonb
                           FOR UPDATE""",
                        restaurant_id, json.dumps([dish_name])
                    )
                    for row in rows:
                        available = float(row["current_stock"])
                        updated = await conn.fetchrow(
                            """UPDATE inventory
                               SET current_stock = current_stock - $1,
                                   updated_at    = NOW()
                               WHERE id = $2
                                 AND current_stock >= $1
                               RETURNING current_stock""",
                            qty, row["id"]
                        )
                        if updated is None:
                            raise InsufficientStockError(
                                sku=dish_name,
                                requested=qty,
                                available=available,
                            )
                        new_stock = float(updated["current_stock"])
                        await conn.execute(
                            """INSERT INTO inventory_history
                               (inventory_id, quantity_delta, stock_after, reason)
                               VALUES ($1, $2, $3, 'orden_confirmada')""",
                            row["id"], -qty, new_stock
                        )
                        dishes = row["linked_dishes"]
                        if isinstance(dishes, str):
                            dishes = json.loads(dishes)
                        if new_stock <= float(row["min_stock"] or 0) and dishes:
                            await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)


# ── Escandallos / Recipes ─────────────────────────────────────────────────────

async def db_upsert_dish_recipe(restaurant_id: int, dish_name: str, lines: list) -> list:
    """
    Reemplaza el escandallo completo de un plato.
    lines = [{"ingredient_id": int, "quantity": float}, ...]
    Pasar lines=[] para eliminar la receta (plato vuelve a estar available).

    Tras guardar, re-evalúa la disponibilidad del plato en menu_availability
    comprobando si algún ingrediente nuevo tiene stock <= min_stock (Fase 5c).
    Si el escandallo se elimina (lines=[]), el plato se marca available porque
    sin receta no hay restricción de stock.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    async with _tenant_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM dish_recipes WHERE restaurant_id=$1 AND dish_name=$2",
                restaurant_id, dish_name
            )
            for line in lines:
                await conn.execute(
                    """INSERT INTO dish_recipes (restaurant_id, dish_name, ingredient_id, quantity)
                       VALUES ($1, $2, $3, $4)""",
                    restaurant_id, dish_name,
                    int(line["ingredient_id"]), float(line["quantity"])
                )

            # Re-evaluate availability after recipe change (Fase 5c)
            if not lines:
                # No recipe → no stock constraint → mark available
                await _sync_dish_availability_conn(conn, [dish_name], True, restaurant_id)
                log.info("inventory.recipe_deleted_dish_available", dish=dish_name, restaurant_id=restaurant_id)
            else:
                # Check if any new ingredient is already depleted
                depleted = await conn.fetchval(
                    """SELECT COUNT(*)
                       FROM dish_recipes r
                       JOIN inventory i ON i.id = r.ingredient_id
                       WHERE r.dish_name = $1 AND r.restaurant_id = $2
                         AND i.current_stock <= i.min_stock""",
                    dish_name, restaurant_id,
                )
                available = (depleted == 0)
                await _sync_dish_availability_conn(conn, [dish_name], available, restaurant_id)
                log.info(
                    "inventory.recipe_upserted_availability_synced",
                    dish=dish_name,
                    available=available,
                    depleted_ingredients=depleted,
                    restaurant_id=restaurant_id,
                )

    return await db_get_dish_recipe(restaurant_id, dish_name)


async def db_get_dish_recipe(restaurant_id: int, dish_name: str) -> list:
    """Devuelve las líneas de ingredientes de un plato, con costo por línea."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT r.id, r.ingredient_id, r.quantity,
                   i.name AS ingredient_name, i.unit, i.cost_per_unit,
                   ROUND((r.quantity * i.cost_per_unit)::numeric, 2) AS line_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1 AND r.dish_name = $2
            ORDER BY i.name
        """, restaurant_id, dish_name)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_recipes(restaurant_id: int) -> list:
    """Lista todos los escandallos con food cost total por plato."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT r.dish_name,
                   COUNT(*) AS ingredient_count,
                   ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]


async def db_delete_dish_recipe(restaurant_id: int, dish_name: str):
    """Elimina todos los ingredientes del escandallo de un plato."""
    async with _tenant_connection() as conn:
        await conn.execute(
            "DELETE FROM dish_recipes WHERE restaurant_id=$1 AND dish_name=$2",
            restaurant_id, dish_name
        )


async def db_get_food_costs(restaurant_id: int) -> list:
    """
    Devuelve el Food Cost de cada plato que tiene escandallo definido.
    Incluye desglose por ingrediente para que el dueño vea de dónde viene el costo.
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT
                r.dish_name,
                ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost,
                json_agg(
                    json_build_object(
                        'ingredient',    i.name,
                        'unit',          i.unit,
                        'quantity',      r.quantity,
                        'cost_per_unit', i.cost_per_unit,
                        'line_cost',     ROUND((r.quantity * i.cost_per_unit)::numeric, 2)
                    ) ORDER BY i.name
                ) AS breakdown
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]
