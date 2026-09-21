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
from app.services.logging import get_logger
from app.services.money import to_decimal

log = get_logger(__name__)


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

async def _sync_dish_availability_conn(conn, dish_names: list, available: bool, restaurant_id: int,
                                       location_id: int = None):
    """Enables or disables dishes AT ONE SEDE, on an existing connection.

    Stock is per sede (0090) and so is sold-out state (0091): a sede running
    out of tomatoes must not take the dish off the other sedes' menus. A
    caller with no sede to name is a bug, not a case to default away —
    `location_id=None` skips the write and says so in the log rather than
    re-creating the org-wide row 0091 removed.
    """
    if location_id is None:
        log.warning(
            "menu_availability.sync_without_sede",
            restaurant_id=restaurant_id, dishes=dish_names,
        )
        return
    for name in dish_names:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, org_id, location_id, available, updated_at)
               VALUES ($1, $2, $3, $4, NOW())
               ON CONFLICT (org_id, location_id, dish_name)
               DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
            name, restaurant_id, location_id, available
        )


async def _sync_dish_availability(dish_names: list, available: bool, restaurant_id: int,
                                  location_id: int = None):
    """Enables or disables dishes in menu_availability based on stock, per sede."""
    if not dish_names:
        return
    async with _tenant_connection() as conn:
        await _sync_dish_availability_conn(conn, dish_names, available, restaurant_id, location_id)


async def _sync_ingredient_dishes_conn(
    conn, ingredient_id: int, new_stock: float, min_stock: float, restaurant_id: int,
    location_id: int = None,
) -> None:
    """
    When an ingredient drops to <= min_stock, finds ALL dishes that use it
    via dish_recipes and marks them unavailable in menu_availability.
    Also syncs legacy linked_dishes for the same ingredient.

    Call within an open transaction (conn already holds a lock on the ingredient).
    Does not fail silently: if the menu_availability INSERT errors,
    it propagates to the caller so the transaction rolls back.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    if new_stock > min_stock:
        # Stock replenished — re-evaluate affected dishes to mark them available
        # Only if ALL their other ingredients also have stock > min_stock.
        await _recheck_dishes_for_ingredient_conn(conn, ingredient_id, restaurant_id, location_id)
        return

    # Stock depleted or at minimum — mark dishes as unavailable
    recipe_rows = await conn.fetch(
        "SELECT dish_name FROM dish_recipes WHERE ingredient_id = $1 AND org_id = $2",
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
        await _sync_dish_availability_conn(conn, dish_names, False, restaurant_id, location_id)


async def _resync_dish_for_every_sede_conn(
    conn, restaurant_id: int, dish_name: str, force_available: bool = None
) -> None:
    """Re-evaluate one dish's availability at EVERY sede of the org.

    Recipes are org-level (a dish is made the same way everywhere) but the
    stock they consume is not, so editing a recipe has to be answered once
    per sede against that sede's own fridge. `force_available` short-circuits
    the stock check for the "recipe deleted, no constraint left" case.
    """
    sedes = await conn.fetch(
        "SELECT id FROM locations WHERE org_id = $1", restaurant_id,
    )
    for sede in sedes:
        if force_available is not None:
            available = force_available
        else:
            depleted = await conn.fetchval(
                """SELECT COUNT(DISTINCT r.ingredient_id)
                     FROM dish_recipes r
                     JOIN inventory src ON src.id = r.ingredient_id
                     JOIN inventory tgt
                       ON tgt.org_id = src.org_id
                      AND lower(tgt.name) = lower(src.name)
                      AND (tgt.location_id = $3 OR tgt.location_id IS NULL)
                    WHERE r.dish_name = $1 AND r.org_id = $2
                      AND tgt.current_stock <= tgt.min_stock""",
                dish_name, restaurant_id, sede["id"],
            )
            available = (depleted == 0)
        await _sync_dish_availability_conn(
            conn, [dish_name], available, restaurant_id, sede["id"],
        )


async def _recheck_dishes_for_ingredient_conn(
    conn, ingredient_id: int, restaurant_id: int, location_id: int = None
) -> None:
    """
    After restocking, re-evaluates each dish that uses this ingredient AT ONE
    SEDE. A dish becomes available again only if ALL its ingredients have
    current_stock > min_stock in THAT sede's own inventory.

    The recipe is org-level and names one ingredient row; the row that
    matters is the one in this sede, matched by name (the rule in this
    module's header note). Checking `inventory i ON i.id = r.ingredient_id`
    like before answered "does SOME sede have stock", which is how a sede
    with an empty fridge could keep selling the dish.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    if location_id is None:
        log.warning(
            "menu_availability.recheck_without_sede",
            restaurant_id=restaurant_id, ingredient_id=ingredient_id,
        )
        return

    recipe_rows = await conn.fetch(
        "SELECT dish_name FROM dish_recipes WHERE ingredient_id = $1 AND org_id = $2",
        ingredient_id, restaurant_id,
    )
    for row in recipe_rows:
        dish_name = row["dish_name"]
        # DISTINCT on ingredient_id: a sede holding two rows with the same
        # product name must not count as two satisfied ingredients.
        total = await conn.fetchval(
            "SELECT COUNT(DISTINCT ingredient_id) FROM dish_recipes "
            "WHERE dish_name = $1 AND org_id = $2",
            dish_name, restaurant_id,
        )
        ok = await conn.fetchval(
            """SELECT COUNT(DISTINCT r.ingredient_id)
                 FROM dish_recipes r
                 JOIN inventory src ON src.id = r.ingredient_id
                 JOIN inventory tgt
                   ON tgt.org_id = src.org_id
                  AND lower(tgt.name) = lower(src.name)
                  AND (tgt.location_id = $3 OR tgt.location_id IS NULL)
                WHERE r.dish_name = $1 AND r.org_id = $2
                  AND tgt.current_stock > tgt.min_stock""",
            dish_name, restaurant_id, location_id,
        )
        available = (ok == total)
        log.info(
            "inventory.recheck_dish",
            dish=dish_name,
            available=available,
            ok_ingredients=ok,
            total_ingredients=total,
            restaurant_id=restaurant_id,
            location_id=location_id,
        )
        await _sync_dish_availability_conn(
            conn, [dish_name], available, restaurant_id, location_id,
        )


# ── DDL init (legacy stubs — schema is fully managed by Alembic) ─────────────
# Tables: nps_responses, inventory, inventory_history, nps_waiting → 0001_initial_schema.py
# Table:  dish_recipes → 0001_initial_schema.py
# Indexes: idx_table_orders_base, idx_table_orders_station → 0001_initial_schema.py
# Index:   idx_rest_tables_lookup → 0020_missing_runtime_tables.py
# Columns: conversations.created_at, restaurants.google_maps_url → 0001_initial_schema.py

# ── Inventory CRUD ────────────────────────────────────────────────────────────

# ── Per-sede stock (PM 2026-09-20) ───────────────────────────────────────────
#
# Stock belongs to ONE sede: "el inventario es uno por sede". Two rules run
# through every query below.
#
# 1. A sede-scoped read is `location_id = $n OR location_id IS NULL`. The NULL
#    half is not laziness — migration 0090 backfills what exists, but an org
#    with no `locations` row cannot be backfilled, and a stock list that
#    silently drops rows is worse than one that shows an unassigned item.
#
# 2. "The same product at another sede" is matched by `lower(name)` within the
#    org. Inventory rows are per-sede, so sede A's "Tomate" and sede B's
#    "Tomate" are two different rows with two different ids; name is the only
#    thing that ties them together. Used by transfers AND by the order
#    deduction path, which has to turn a recipe's ingredient_id (a row in
#    whatever sede it was defined in) into the row of the sede that is
#    actually cooking. One rule, both places.

async def db_get_inventory(restaurant_id: int, location_id: int | None = None) -> list:
    """Stock of ONE sede. None = every sede, which only owner/admin ever get
    (app/routes/deps.py::resolve_sede_filter decides that, not this)."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory
                WHERE org_id = $1
                  AND ($2::bigint IS NULL OR location_id = $2 OR location_id IS NULL)
                ORDER BY name ASC""",
            restaurant_id, location_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_create_inventory_item(restaurant_id: int, name: str, unit: str,
                                    current_stock: float, min_stock: float,
                                    linked_dishes: list, cost_per_unit: float = 0,
                                    location_id: int | None = None) -> dict:
    """Create a stock item IN a sede.

    `location_id` is required in practice — the route refuses a create
    without one, so an owner managing several sedes has to pick which fridge
    they are filling instead of quietly adding to a shared pool that no
    longer exists.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO inventory
               (org_id, location_id, name, unit, current_stock, min_stock,
                linked_dishes, cost_per_unit)
               VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
               RETURNING *""",
            restaurant_id, location_id, name, unit, current_stock, min_stock,
            json.dumps(linked_dishes), cost_per_unit
        )
        item = _serialize(dict(row))
        # Si el stock es 0, desactivar platos vinculados
        if current_stock <= 0:
            await _sync_dish_availability(linked_dishes, False, restaurant_id, location_id)
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

        restaurant_id = existing["org_id"]

        # The item's OWN sede — editing sede A's stock must not re-open or
        # close the dish at sede B.
        await _sync_dish_availability(
            dishes, new_stock > 0, restaurant_id, existing["location_id"],
        )
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
               WHERE id = $2 AND org_id = $3
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
        await _sync_dish_availability(
            dishes, float(item["current_stock"]) > 0, restaurant_id, item.get("location_id"),
        )
        return item


async def db_get_inventory_item(item_id: int) -> dict | None:
    """Gets an inventory item by ID. Tenant-scoped via RLS.

    `location_id` is in the projection so callers can check the row belongs
    to a sede they may touch — without it every ownership check in
    app/routes/inventory.py could only compare org_id, which is how a cook
    at sede A could edit sede B's stock.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT id, org_id, location_id, name, unit, current_stock, min_stock, "
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


async def db_get_inventory_alerts(restaurant_id: int, location_id: int | None = None) -> list:
    """Low-stock items of ONE sede — a kitchen is alerted about its own
    fridge, not about what another sede ran out of."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory
               WHERE org_id = $1
                 AND ($2::bigint IS NULL OR location_id = $2 OR location_id IS NULL)
                 AND current_stock <= min_stock
               ORDER BY current_stock ASC""",
            restaurant_id, location_id,
        )
    return [_serialize(dict(r)) for r in rows]


class SedeTransferError(Exception):
    """Raised when a transfer's own arguments make no sense (same sede, no
    quantity, destination outside the org). Distinct from
    InsufficientStockError, which is about the stock itself — the route maps
    one to 400 and the other to 409."""


async def db_transfer_inventory(
    org_id: int,
    item_id: int,
    to_location_id: int,
    quantity,
    note: str = "",
) -> dict:
    """Move stock of one product from its sede to another sede of the same org.

    PM 2026-09-20: "se puede hacer intercambios de inventario por sede".

    The destination row is the SAME product at the other sede, matched by
    `lower(name)` within the org (see the module note). If that sede has
    never stocked it, the row is created there with the source's unit,
    min_stock, linked_dishes and cost — receiving a product you do not have
    yet is the normal case for a transfer, and forcing the owner to
    pre-create an empty item first would be busywork.

    Everything happens in ONE transaction with the source row locked, so two
    transfers of the last 5kg cannot both succeed. A transfer that would
    overdraw the source raises InsufficientStockError and writes nothing.

    Deliberately does NOT touch dish availability: `menu_availability` is
    keyed `(dish_name, org_id)`, so flipping it here would enable or disable
    a dish for the WHOLE organization because one sede's stock moved. A
    transfer does not change the org's total stock anyway. Per-sede dish
    availability is a separate gap (see docs/claude/rls-multitenant.md).

    # Requires active tenant_scope(org_id).
    """
    qty = to_decimal(quantity)
    if qty <= 0:
        raise SedeTransferError("La cantidad a trasladar debe ser mayor que cero")

    async with _tenant_connection() as conn:
        src = await conn.fetchrow(
            "SELECT * FROM inventory WHERE id = $1 AND org_id = $2 FOR UPDATE",
            item_id, org_id,
        )
        if not src:
            raise SedeTransferError("Producto no encontrado")
        if src["location_id"] is not None and int(src["location_id"]) == int(to_location_id):
            raise SedeTransferError("El origen y el destino son la misma sede")

        dest_exists = await conn.fetchval(
            "SELECT 1 FROM locations WHERE id = $1 AND org_id = $2",
            to_location_id, org_id,
        )
        if not dest_exists:
            raise SedeTransferError("La sede de destino no pertenece a tu organización")

        moved = await conn.fetchrow(
            """UPDATE inventory
                  SET current_stock = current_stock - $1, updated_at = NOW()
                WHERE id = $2 AND current_stock >= $1
            RETURNING current_stock""",
            qty, item_id,
        )
        if moved is None:
            raise InsufficientStockError(
                sku=src["name"],
                requested=qty,
                available=to_decimal(src["current_stock"]),
            )

        dest = await conn.fetchrow(
            """SELECT * FROM inventory
                WHERE org_id = $1 AND location_id = $2 AND lower(name) = lower($3)
                ORDER BY id
                LIMIT 1
                FOR UPDATE""",
            org_id, to_location_id, src["name"],
        )
        if dest is None:
            dest = await conn.fetchrow(
                """INSERT INTO inventory
                   (org_id, location_id, name, unit, current_stock, min_stock,
                    linked_dishes, cost_per_unit)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                   RETURNING *""",
                org_id, to_location_id, src["name"], src["unit"], qty,
                src["min_stock"], src["linked_dishes"], src["cost_per_unit"],
            )
        else:
            dest = await conn.fetchrow(
                """UPDATE inventory
                      SET current_stock = current_stock + $1, updated_at = NOW()
                    WHERE id = $2
                RETURNING *""",
                qty, dest["id"],
            )

        reason_out = f"traslado_salida:{to_location_id}"
        reason_in = f"traslado_entrada:{src['location_id']}"
        if note.strip():
            reason_out = f"{reason_out} {note.strip()[:120]}"
            reason_in = f"{reason_in} {note.strip()[:120]}"

        await conn.execute(
            """INSERT INTO inventory_history (inventory_id, quantity_delta, stock_after, reason)
               VALUES ($1, $2, $3, $4)""",
            item_id, -qty, moved["current_stock"], reason_out,
        )
        await conn.execute(
            """INSERT INTO inventory_history (inventory_id, quantity_delta, stock_after, reason)
               VALUES ($1, $2, $3, $4)""",
            dest["id"], qty, dest["current_stock"], reason_in,
        )

    log.info(
        "inventory.transferred",
        org_id=org_id, item_id=item_id, to_location_id=to_location_id,
        dest_item_id=dest["id"],
    )
    return {
        "source": _serialize({**dict(src), "current_stock": moved["current_stock"]}),
        "destination": _serialize(dict(dest)),
    }


async def db_deduct_inventory_for_order(bot_number: str, items: list,
                                        location_id: int | None = None):
    """
    Deducts stock for each ordered dish, with support for recipes (dish_recipes).
    items = [{"name": "Hamburguesa Clásica", "quantity": 2}, ...]
    Uses SELECT FOR UPDATE inside a transaction to avoid race conditions
    across Railway's 4 workers.
    If no recipe is defined, falls back to legacy linked_dishes behavior.

    NOTE: For delivery/pickup orders, use commit_order_transaction in
    app.repositories.orders_repo, which wraps this together with db_save_order and
    cart cleanup in a single transaction.

    `location_id` is the sede that is cooking: stock is per sede
    (PM 2026-09-20), so a table at sede B must not eat sede A's fridge. The
    recipe's ingredient_id is resolved to the row of THIS sede by name - the
    same rule transfers use, see the module note at the top of this file.
    None keeps the old org-wide behaviour for callers with no sede in hand.

    Raises:
        InsufficientStockError: if an ingredient's stock is insufficient.
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
                       WHERE r.org_id = $1 AND r.dish_name = $2""",
                    restaurant_id, dish_name
                )

                if recipe_rows:
                    # Lock THIS sede's row for each recipe ingredient (matched
                    # by name across sedes). With location_id NULL the join
                    # collapses to src = tgt, i.e. the old behaviour.
                    ingredient_ids = [r["ingredient_id"] for r in recipe_rows]
                    locked = await conn.fetch(
                        """SELECT src.id AS recipe_ingredient_id,
                                  tgt.id, tgt.current_stock, tgt.min_stock,
                                  tgt.linked_dishes
                             FROM inventory src
                             JOIN inventory tgt
                               ON tgt.org_id = src.org_id
                              AND lower(tgt.name) = lower(src.name)
                              AND ($2::bigint IS NULL
                                   OR tgt.location_id = $2
                                   OR tgt.location_id IS NULL)
                            WHERE src.id = ANY($1::int[])
                              AND src.org_id = $3
                         ORDER BY tgt.id
                              FOR UPDATE OF tgt""",
                        ingredient_ids, location_id, restaurant_id
                    )
                    locked_map = {}
                    for r in locked:
                        locked_map.setdefault(r["recipe_ingredient_id"], r)

                    for rline in recipe_rows:
                        ing_id    = rline["ingredient_id"]
                        deduct    = float(rline["recipe_qty"]) * qty
                        inv       = locked_map.get(ing_id)
                        if not inv:
                            continue
                        # This sede's row is what moves, not the recipe's.
                        ing_id = inv["id"]
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
                        await _sync_ingredient_dishes_conn(
                            conn, ing_id, new_stock, min_stock_val, restaurant_id, location_id,
                        )
                        # Also sync legacy linked_dishes on the same ingredient
                        if new_stock <= min_stock_val:
                            dishes = inv["linked_dishes"]
                            if isinstance(dishes, str):
                                dishes = json.loads(dishes)
                            if dishes:
                                await _sync_dish_availability_conn(
                                    conn, dishes, False, restaurant_id, location_id,
                                )

                else:
                    # ── 2. Fallback legacy: linked_dishes ────────────────
                    # NOTE (P0 found 2026-09, fixed here): the pool's jsonb
                    # codec (app/services/database.py get_pool(), encoder=
                    # json.dumps) already serializes a Python list into the
                    # $N::jsonb parameter. Passing a PRE-dumped json.dumps()
                    # string here double-encoded it into a jsonb STRING
                    # SCALAR wrapping the array text, so `linked_dishes @>
                    # $2::jsonb` (array containment) silently NEVER matched
                    # any row — restaurants using legacy linked_dishes
                    # (no dish_recipes escandallo) never had stock enforced
                    # or decremented for their orders. Pass the raw list so
                    # the codec encodes it exactly once.
                    rows = await conn.fetch(
                        """SELECT id, current_stock, linked_dishes, min_stock
                           FROM inventory
                           WHERE org_id = $1
                             AND linked_dishes @> $2::jsonb
                           FOR UPDATE""",
                        restaurant_id, [dish_name]
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
                            await _sync_dish_availability_conn(
                                conn, dishes, False, restaurant_id, location_id,
                            )


# ── Recipes ─────────────────────────────────────────────────────

async def db_upsert_dish_recipe(restaurant_id: int, dish_name: str, lines: list) -> list:
    """
    Replaces a dish's full recipe.
    lines = [{"ingredient_id": int, "quantity": float}, ...]
    Pass lines=[] to remove the recipe (dish becomes available again).

    After saving, re-evaluates the dish's availability in menu_availability
    by checking whether any new ingredient has stock <= min_stock (Phase 5c).
    If the recipe is deleted (lines=[]), the dish is marked available because
    without a recipe there is no stock constraint.
    """
    from app.services.logging import get_logger
    log = get_logger(__name__)

    async with _tenant_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM dish_recipes WHERE org_id=$1 AND dish_name=$2",
                restaurant_id, dish_name
            )
            for line in lines:
                await conn.execute(
                    """INSERT INTO dish_recipes (org_id, dish_name, ingredient_id, quantity)
                       VALUES ($1, $2, $3, $4)""",
                    restaurant_id, dish_name,
                    int(line["ingredient_id"]), float(line["quantity"])
                )

            # Re-evaluate availability after recipe change (Phase 5c)
            # The recipe is org-level; its CONSEQUENCE is not. Answer
            # "is this dish available" once per sede, against that sede's
            # own stock, instead of writing one org-wide verdict.
            if not lines:
                # No recipe → no stock constraint → available everywhere
                await _resync_dish_for_every_sede_conn(
                    conn, restaurant_id, dish_name, force_available=True,
                )
                log.info("inventory.recipe_deleted_dish_available", dish=dish_name, restaurant_id=restaurant_id)
            else:
                await _resync_dish_for_every_sede_conn(conn, restaurant_id, dish_name)
                log.info(
                    "inventory.recipe_upserted_availability_synced",
                    dish=dish_name,
                    restaurant_id=restaurant_id,
                )

    return await db_get_dish_recipe(restaurant_id, dish_name)


async def db_get_dish_recipe(restaurant_id: int, dish_name: str) -> list:
    """Returns a dish's ingredient lines, with per-line cost."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT r.id, r.ingredient_id, r.quantity,
                   i.name AS ingredient_name, i.unit, i.cost_per_unit,
                   ROUND((r.quantity * i.cost_per_unit)::numeric, 2) AS line_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.org_id = $1 AND r.dish_name = $2
            ORDER BY i.name
        """, restaurant_id, dish_name)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_recipes(restaurant_id: int) -> list:
    """Lists all recipes with total food cost per dish."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT r.dish_name,
                   COUNT(*) AS ingredient_count,
                   ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.org_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]


async def db_delete_dish_recipe(restaurant_id: int, dish_name: str):
    """Deletes all ingredients from a dish's recipe."""
    async with _tenant_connection() as conn:
        await conn.execute(
            "DELETE FROM dish_recipes WHERE org_id=$1 AND dish_name=$2",
            restaurant_id, dish_name
        )


async def db_get_food_costs(restaurant_id: int) -> list:
    """
    Returns the Food Cost of each dish that has a recipe defined.
    Includes a per-ingredient breakdown so the owner can see where the cost comes from.
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
            WHERE r.org_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]
