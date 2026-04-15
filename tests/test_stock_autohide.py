"""
tests/test_stock_autohide.py
Fase 5c — Auto-hide de platos cuando el stock de un ingrediente se agota.

Cubre:
  1. Plato con receta: stock OK → disponible
  2. Stock agotado → _sync_ingredient_dishes_conn marca el plato unavailable
  3. Stock repuesto → _recheck_dishes_for_ingredient_conn lo vuelve available
  4. Plato sin receta: disponibilidad NO se toca por stock
  5. Receta multi-ingrediente: basta con que UNO se agote → no disponible
  6. db_upsert_dish_recipe con ingrediente depletado → plato unavailable
  7. db_upsert_dish_recipe con lines=[] → plato vuelve available
  8. commit_order_transaction invoca el sync por la ruta dish_recipes (orders_repo)

No requiere base de datos real — todos los accesos a DB se mockean.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.services.tenant_context import tenant_scope, bypass_tenant_scope


# ── helpers ───────────────────────────────────────────────────────────────────

def _row(d: dict):
    """Mimics an asyncpg Record."""
    r = MagicMock()
    r.__iter__ = lambda s: iter(d.items())
    r.keys = lambda: d.keys()
    r.__getitem__ = lambda s, k: d[k]
    r.get = lambda k, default=None: d.get(k, default)
    return r


def _pool(conn):
    p = AsyncMock()
    p.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    return p


def _tx_cm():
    return MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))


# ══════════════════════════════════════════════════════════════════════════════
# 1. _sync_ingredient_dishes_conn — stock OK → no mark unavailable
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_ingredient_no_op_when_stock_ok():
    """new_stock > min_stock → delegates to _recheck, NOT marks unavailable."""
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    recheck_calls = []

    async def fake_recheck(c, ing_id, restaurant_id):
        recheck_calls.append((ing_id, restaurant_id))

    with patch.object(inv, "_recheck_dishes_for_ingredient_conn", fake_recheck):
        await inv._sync_ingredient_dishes_conn(conn, ingredient_id=5, new_stock=3.0, min_stock=1.0, restaurant_id=1)

    assert recheck_calls == [(5, 1)]
    conn.fetch.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 2. _sync_ingredient_dishes_conn — stock depleted → marks dishes unavailable
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_sync_ingredient_marks_dishes_unavailable_on_depletion():
    """new_stock <= min_stock → queries dish_recipes and marks dishes unavailable."""
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        _row({"dish_name": "Bandeja Paisa"}),
        _row({"dish_name": "Arroz con Pollo"}),
    ])

    sync_calls = []

    async def fake_sync_conn(c, dish_names, available, restaurant_id):
        sync_calls.append((sorted(dish_names), available, restaurant_id))

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync_conn):
        await inv._sync_ingredient_dishes_conn(
            conn, ingredient_id=7, new_stock=0.0, min_stock=0.5, restaurant_id=1
        )

    assert len(sync_calls) == 1
    assert sync_calls[0][0] == ["Arroz con Pollo", "Bandeja Paisa"]
    assert sync_calls[0][1] is False
    assert sync_calls[0][2] == 1


@pytest.mark.asyncio
async def test_sync_ingredient_no_dishes_in_recipe_is_noop():
    """If ingredient is not referenced by any dish_recipe, nothing is written."""
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    sync_calls = []

    async def fake_sync_conn(c, dish_names, available, restaurant_id):
        sync_calls.append(dish_names)

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync_conn):
        await inv._sync_ingredient_dishes_conn(
            conn, ingredient_id=99, new_stock=0.0, min_stock=1.0, restaurant_id=1
        )

    assert sync_calls == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. _recheck_dishes_for_ingredient_conn — all ok → marks available
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_recheck_marks_dish_available_when_all_ingredients_ok():
    """After restock, dish becomes available when all ingredients are above min."""
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    # ingredient 5 is referenced by "Pizza"
    conn.fetch = AsyncMock(return_value=[_row({"dish_name": "Pizza"})])
    # fetchval calls: total=2, ok=2
    conn.fetchval = AsyncMock(side_effect=[2, 2])

    sync_calls = []

    async def fake_sync(c, names, avail, rid):
        sync_calls.append((names, avail))

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync):
        await inv._recheck_dishes_for_ingredient_conn(conn, ingredient_id=5, restaurant_id=1)

    assert len(sync_calls) == 1
    assert sync_calls[0][0] == ["Pizza"]
    assert sync_calls[0][1] is True  # available


@pytest.mark.asyncio
async def test_recheck_keeps_dish_unavailable_if_another_ingredient_depleted():
    """
    If only one of two ingredients was restocked, but the other is still low,
    the dish should remain unavailable.
    """
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[_row({"dish_name": "Sopa del Mar"})])
    # total=2, ok=1 → still one depleted ingredient
    conn.fetchval = AsyncMock(side_effect=[2, 1])

    sync_calls = []

    async def fake_sync(c, names, avail, rid):
        sync_calls.append((names, avail))

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync):
        await inv._recheck_dishes_for_ingredient_conn(conn, ingredient_id=3, restaurant_id=1)

    assert sync_calls[0][1] is False  # still unavailable


# ══════════════════════════════════════════════════════════════════════════════
# 4. db_deduct_inventory_for_order — recipe path triggers auto-hide
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deduct_recipe_path_triggers_ingredient_sync():
    """
    When a delivery order deducts an ingredient via dish_recipes to 0,
    _sync_ingredient_dishes_conn is called with new_stock <= min_stock.
    """
    from app.services import database as db
    import app.repositories.inventory_repo as inv

    restaurant = {"id": 1}
    recipe_row = _row({"ingredient_id": 10, "recipe_qty": 2.0})
    locked_row = _row({
        "id": 10, "current_stock": 2.0, "min_stock": 0.5,
        "linked_dishes": json.dumps([]),
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[
        [recipe_row],  # dish_recipes query
        [locked_row],  # SELECT FOR UPDATE
    ])
    # UPDATE returns new_stock = 0.0
    mock_conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 0.0))
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = _tx_cm()

    sync_calls = []

    async def fake_sync_ingredient(conn, ingredient_id, new_stock, min_stock, restaurant_id):
        sync_calls.append({"ingredient_id": ingredient_id, "new_stock": new_stock, "min_stock": min_stock})

    with (
        patch.object(db, "get_pool", AsyncMock(return_value=_pool(mock_conn))),
        patch.object(db, "db_get_restaurant_by_phone", AsyncMock(return_value=restaurant)),
        patch.object(inv, "_sync_ingredient_dishes_conn", fake_sync_ingredient),
    ):
        with tenant_scope(1):
            await db.db_deduct_inventory_for_order(
                bot_number="+57300",
                items=[{"name": "Bandeja Paisa", "quantity": 1}],
            )

    assert len(sync_calls) == 1
    assert sync_calls[0]["ingredient_id"] == 10
    assert sync_calls[0]["new_stock"] == 0.0
    assert sync_calls[0]["min_stock"] == 0.5


# ══════════════════════════════════════════════════════════════════════════════
# 5. Multi-ingredient: ANY depleted → dish unavailable
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_multi_ingredient_any_depleted_marks_unavailable():
    """
    _recheck_dishes_for_ingredient_conn: if 1 of 3 ingredients is depleted (ok < total),
    dish remains unavailable.
    """
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[_row({"dish_name": "Cazuela de Mariscos"})])
    # total=3, ok=2 → still one depleted
    conn.fetchval = AsyncMock(side_effect=[3, 2])

    sync_calls = []

    async def fake_sync(c, names, avail, rid):
        sync_calls.append(avail)

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync):
        await inv._recheck_dishes_for_ingredient_conn(conn, ingredient_id=1, restaurant_id=1)

    assert sync_calls[0] is False


@pytest.mark.asyncio
async def test_multi_ingredient_all_restocked_marks_available():
    """All 3 ingredients ok after restock → dish becomes available."""
    import app.repositories.inventory_repo as inv

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[_row({"dish_name": "Cazuela de Mariscos"})])
    # total=3, ok=3
    conn.fetchval = AsyncMock(side_effect=[3, 3])

    sync_calls = []

    async def fake_sync(c, names, avail, rid):
        sync_calls.append(avail)

    with patch.object(inv, "_sync_dish_availability_conn", fake_sync):
        await inv._recheck_dishes_for_ingredient_conn(conn, ingredient_id=1, restaurant_id=1)

    assert sync_calls[0] is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. db_upsert_dish_recipe — depleted ingredient → marks dish unavailable
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_upsert_recipe_with_depleted_ingredient_marks_unavailable():
    """
    When a recipe is saved and one of its ingredients already has stock <= min_stock,
    the dish is immediately marked unavailable.
    """
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    # fetchval calls: set_config #1 (tenant), depleted count, set_config #2 (db_get_dish_recipe)
    mock_conn.fetchval = AsyncMock(side_effect=[None, 1, None])
    # fetch for db_get_dish_recipe (called at end)
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.transaction = _tx_cm()

    sync_calls = []

    async def fake_sync_conn(conn, dish_names, available, restaurant_id):
        sync_calls.append((dish_names, available))

    import app.repositories.inventory_repo as inv
    with (
        patch.object(db, "get_pool", AsyncMock(return_value=_pool(mock_conn))),
        patch.object(inv, "_sync_dish_availability_conn", fake_sync_conn),
    ):
        with tenant_scope(1):
            await db.db_upsert_dish_recipe(
                restaurant_id=1,
                dish_name="Hamburguesa Especial",
                lines=[{"ingredient_id": 3, "quantity": 0.2}],
            )

    # Should have been called with available=False because depleted=1
    assert any(call[0] == ["Hamburguesa Especial"] and call[1] is False for call in sync_calls)


@pytest.mark.asyncio
async def test_upsert_recipe_all_stock_ok_marks_available():
    """All ingredients have stock > min_stock → dish is marked available."""
    from app.services import database as db
    import app.repositories.inventory_repo as inv

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    # fetchval calls: set_config #1 (tenant), depleted count=0, set_config #2 (db_get_dish_recipe)
    mock_conn.fetchval = AsyncMock(side_effect=[None, 0, None])
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.transaction = _tx_cm()

    sync_calls = []

    async def fake_sync_conn(conn, dish_names, available, restaurant_id):
        sync_calls.append((dish_names, available))

    with (
        patch.object(db, "get_pool", AsyncMock(return_value=_pool(mock_conn))),
        patch.object(inv, "_sync_dish_availability_conn", fake_sync_conn),
    ):
        with tenant_scope(1):
            await db.db_upsert_dish_recipe(
                restaurant_id=1,
                dish_name="Pizza Margarita",
                lines=[{"ingredient_id": 5, "quantity": 0.1}],
            )

    assert any(call[0] == ["Pizza Margarita"] and call[1] is True for call in sync_calls)


# ══════════════════════════════════════════════════════════════════════════════
# 7. db_upsert_dish_recipe lines=[] → dish vuelve available (sin receta)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_upsert_recipe_empty_lines_marks_dish_available():
    """
    Passing lines=[] deletes the recipe. Without a recipe, no stock constraint
    exists, so the dish must be marked available.
    """
    from app.services import database as db
    import app.repositories.inventory_repo as inv

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = _tx_cm()

    sync_calls = []

    async def fake_sync_conn(conn, dish_names, available, restaurant_id):
        sync_calls.append((dish_names, available))

    with (
        patch.object(db, "get_pool", AsyncMock(return_value=_pool(mock_conn))),
        patch.object(inv, "_sync_dish_availability_conn", fake_sync_conn),
    ):
        # Use bypass_tenant_scope so conn.fetchval is not called by the tenant
        # infrastructure, preserving the assertion that business logic avoids fetchval
        # when lines=[] (no depleted check needed).
        with bypass_tenant_scope("test: empty lines upsert"):
            await db.db_upsert_dish_recipe(
                restaurant_id=1,
                dish_name="Plato sin receta",
                lines=[],
            )

    assert any(call[0] == ["Plato sin receta"] and call[1] is True for call in sync_calls)
    # fetchval should NOT have been called (no depleted check needed)
    mock_conn.fetchval.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 8. commit_order_transaction (orders_repo) calls _sync_ingredient_dishes_conn
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deduct_inventory_in_tx_calls_ingredient_sync():
    """
    _deduct_inventory_in_tx (the inner function used by commit_order_transaction)
    must call _sync_ingredient_dishes_conn for the dish_recipes path.
    We test the internal function directly to avoid having to satisfy the full
    order INSERT contract.
    """
    import app.repositories.orders_repo as orders_repo
    import app.repositories.inventory_repo as inv

    recipe_row = _row({"ingredient_id": 20, "recipe_qty": 1.0})
    locked_row = _row({
        "id": 20, "current_stock": 1.0, "min_stock": 0.0,
        "linked_dishes": json.dumps([]),
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[
        [recipe_row],   # dish_recipes query
        [locked_row],   # SELECT FOR UPDATE
    ])
    # UPDATE RETURNING current_stock → 0.0
    mock_conn.fetchrow = AsyncMock(
        return_value=MagicMock(__getitem__=lambda s, k: 0.0)
    )

    sync_calls = []

    async def fake_sync_ingredient(conn, ingredient_id, new_stock, min_stock, restaurant_id):
        sync_calls.append({"ingredient_id": ingredient_id, "new_stock": new_stock})

    with patch.object(inv, "_sync_ingredient_dishes_conn", fake_sync_ingredient):
        await orders_repo._deduct_inventory_in_tx(
            mock_conn,
            restaurant_id=1,
            items=[{"name": "Tamal", "quantity": 1}],
        )

    assert any(c["ingredient_id"] == 20 for c in sync_calls), (
        "Expected _sync_ingredient_dishes_conn to be called for ingredient 20"
    )
