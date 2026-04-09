"""
Tests para FASE 4: Escandallos / Recetas.
Cubre: db_upsert_dish_recipe, db_get_dish_recipe, db_get_food_costs,
       db_deduct_inventory_for_order (recipe path + legacy fallback).
No requiere base de datos ni credenciales reales.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(d: dict):
    """Crea un asyncpg Row-like desde un dict."""
    row = MagicMock()
    row.__iter__ = lambda s: iter(d.items())
    row.keys     = lambda: d.keys()
    row.__getitem__ = lambda s, k: d[k]
    row.get = lambda k, default=None: d.get(k, default)
    return row


def _make_pool(conn):
    mock_pool = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    return mock_pool


# ══════════════════════════════════════════════════════════════════════════════
# 1. db_upsert_dish_recipe
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_upsert_dish_recipe_llama_delete_e_insert():
    """Upsert debe eliminar las líneas previas y volver a insertar."""
    from app.services import database as db

    result_rows = [_make_row({
        "id": 1, "ingredient_id": 10, "quantity": 0.5,
        "ingredient_name": "Queso", "unit": "kg", "cost_per_unit": 20000,
        "line_cost": 10000
    })]

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=result_rows)
    # transaction context manager
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        result = await db.db_upsert_dish_recipe(
            restaurant_id=1,
            dish_name="Pizza",
            lines=[{"ingredient_id": 10, "quantity": 0.5}]
        )

    # DELETE fue llamado
    delete_calls = [c for c in mock_conn.execute.call_args_list
                    if "DELETE" in str(c)]
    assert len(delete_calls) >= 1

    # INSERT fue llamado
    insert_calls = [c for c in mock_conn.execute.call_args_list
                    if "INSERT" in str(c)]
    assert len(insert_calls) >= 1

    # Devuelve las líneas del GET posterior
    assert len(result) == 1
    assert result[0]["ingredient_id"] == 10


@pytest.mark.asyncio
async def test_upsert_dish_recipe_vacio_elimina_escandallo():
    """Pasar lines=[] debe solo ejecutar el DELETE (borrar el escandallo)."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=[])
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        result = await db.db_upsert_dish_recipe(1, "Pizza", [])

    insert_calls = [c for c in mock_conn.execute.call_args_list
                    if "INSERT" in str(c)]
    assert len(insert_calls) == 0
    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. db_get_food_costs
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_food_costs_retorna_lista_con_breakdown():
    """db_get_food_costs debe retornar dish_name, food_cost y breakdown."""
    from app.services import database as db

    breakdown = [{"ingredient": "Queso", "unit": "kg", "quantity": 0.2,
                  "cost_per_unit": 20000, "line_cost": 4000}]
    row = _make_row({
        "dish_name": "Pizza Margarita",
        "food_cost": 4000,
        "breakdown": breakdown
    })

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[row])

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        result = await db.db_get_food_costs(restaurant_id=1)

    assert len(result) == 1
    assert result[0]["dish_name"] == "Pizza Margarita"
    assert float(result[0]["food_cost"]) == 4000
    assert isinstance(result[0]["breakdown"], list)


@pytest.mark.asyncio
async def test_get_food_costs_sin_escandallos_retorna_lista_vacia():
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        result = await db.db_get_food_costs(restaurant_id=99)

    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. db_deduct_inventory_for_order — recipe path (escandallo)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deduct_usa_receta_cuando_existe():
    """
    Si hay líneas en dish_recipes para un plato, debe descontar por ingrediente
    (qty_receta × qty_pedida) y NO usar linked_dishes.
    """
    from app.services import database as db

    restaurant = {"id": 1}

    # recipe row: 0.3 kg de queso por porción
    recipe_row = _make_row({"ingredient_id": 10, "recipe_qty": 0.3})
    # locked inventory row: 5 kg stock
    locked_row = _make_row({
        "id": 10, "current_stock": 5.0, "min_stock": 0.5,
        "linked_dishes": json.dumps(["Pizza"])
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[
        [recipe_row],   # dish_recipes query
        [locked_row],   # SELECT ... FOR UPDATE
    ])
    # fetchrow returns the updated stock after UPDATE ... RETURNING current_stock
    mock_conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 4.4))
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(db, "db_get_restaurant_by_phone",
                     AsyncMock(return_value=restaurant)),
    ):
        await db.db_deduct_inventory_for_order(
            bot_number="+57300", items=[{"name": "Pizza", "quantity": 2}]
        )

    # Debe haber un UPDATE de inventario via fetchrow (uses RETURNING)
    update_calls = [c for c in mock_conn.fetchrow.call_args_list
                    if "UPDATE inventory" in str(c)]
    assert len(update_calls) == 1

    # deduct = recipe_qty * qty = 0.3 * 2 = 0.6
    # args: (sql, deduct, ing_id)
    update_args = update_calls[0].args
    assert abs(float(update_args[1]) - 0.6) < 0.001

    # Historial registrado via execute
    history_calls = [c for c in mock_conn.execute.call_args_list
                     if "inventory_history" in str(c)]
    assert len(history_calls) == 1


@pytest.mark.asyncio
async def test_deduct_usa_linked_dishes_fallback_sin_receta():
    """
    Si dish_recipes no tiene líneas para el plato, cae al comportamiento
    legacy de linked_dishes.
    """
    from app.services import database as db

    restaurant = {"id": 1}

    legacy_row = _make_row({
        "id": 5, "current_stock": 10.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Hamburguesa"])
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[
        [],           # dish_recipes → vacío → fallback
        [legacy_row], # linked_dishes FOR UPDATE
    ])
    # fetchrow returns updated stock after UPDATE ... RETURNING current_stock
    mock_conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 7.0))
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(db, "db_get_restaurant_by_phone",
                     AsyncMock(return_value=restaurant)),
    ):
        await db.db_deduct_inventory_for_order(
            bot_number="+57300",
            items=[{"name": "Hamburguesa", "quantity": 3}]
        )

    # UPDATE uses fetchrow (RETURNING current_stock)
    update_calls = [c for c in mock_conn.fetchrow.call_args_list
                    if "UPDATE inventory" in str(c)]
    assert len(update_calls) == 1
    # args: (sql, qty=3, row_id=5) — deduction amount is 3
    assert abs(float(update_calls[0].args[1]) - 3.0) < 0.001


@pytest.mark.asyncio
async def test_deduct_desactiva_plato_al_agotar_stock():
    """
    Cuando el stock de un ingrediente baja a ≤ min_stock, debe llamar
    a _sync_dish_availability_conn para desactivar los platos vinculados.
    """
    from app.services import database as db
    import app.repositories.inventory_repo as inv_repo

    restaurant = {"id": 1}

    recipe_row = _make_row({"ingredient_id": 7, "recipe_qty": 0.5})
    locked_row = _make_row({
        "id": 7, "current_stock": 0.5, "min_stock": 0.5,
        "linked_dishes": json.dumps(["Sopa del día"])
    })

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[
        [recipe_row],
        [locked_row],
    ])
    # fetchrow returns new_stock = 0.0 (0.5 - 0.5) so new_stock <= min_stock triggers sync
    mock_conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 0.0))
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    sync_calls = []

    async def fake_sync_conn(conn, dish_names, available, restaurant_id):
        sync_calls.append((dish_names, available))

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(db, "db_get_restaurant_by_phone",
                     AsyncMock(return_value=restaurant)),
        patch.object(inv_repo, "_sync_dish_availability_conn", fake_sync_conn),
    ):
        await db.db_deduct_inventory_for_order(
            bot_number="+57300",
            items=[{"name": "Sopa del día", "quantity": 1}]
        )

    # Debe haber llamado a _sync_dish_availability_conn con available=False
    assert len(sync_calls) == 1
    assert sync_calls[0][1] is False
    assert "Sopa del día" in sync_calls[0][0]


@pytest.mark.asyncio
async def test_deduct_restaurante_inexistente_no_hace_nada():
    """Si el restaurante no existe, la función debe retornar sin error."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=[])

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(db, "db_get_restaurant_by_phone",
                     AsyncMock(return_value=None)),
    ):
        # No debe lanzar excepción
        await db.db_deduct_inventory_for_order(
            bot_number="+57000",
            items=[{"name": "Nada", "quantity": 1}]
        )

    mock_conn.execute.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# 4. HTTP routes — /api/inventory/recipes
# ══════════════════════════════════════════════════════════════════════════════

def test_recipe_routes_upsert_y_delete(client, monkeypatch):
    """POST y DELETE de /api/inventory/recipes retornan 200 con datos correctos."""
    from app.services import database as db_mod

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {"username": "admin_test", "restaurant_name": "Rest", "branch_id": 1, "role": "owner"}

    async def mock_get_restaurant(request):
        return {"id": 1, "whatsapp_number": "+57300", "name": "Rest"}

    async def mock_upsert(restaurant_id, dish_name, lines):
        return [{"ingredient_id": 10, "quantity": 0.5, "ingredient_name": "Queso",
                 "unit": "kg", "cost_per_unit": 20000, "line_cost": 10000}]

    async def mock_delete(restaurant_id, dish_name):
        pass

    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr("app.routes.inventory.get_current_restaurant", mock_get_restaurant)
    monkeypatch.setattr(db_mod, "db_upsert_dish_recipe", mock_upsert)
    monkeypatch.setattr(db_mod, "db_delete_dish_recipe", mock_delete)

    # POST
    resp = client.post(
        "/api/inventory/recipes",
        json={"dish_name": "Pizza", "lines": [{"ingredient_id": 10, "quantity": 0.5}]},
        headers={"Authorization": "Bearer fake-token"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["dish_name"] == "Pizza"
    assert len(data["lines"]) == 1

    # DELETE
    resp2 = client.delete(
        "/api/inventory/recipes/Pizza",
        headers={"Authorization": "Bearer fake-token"}
    )
    assert resp2.status_code == 200
    assert resp2.json()["success"] is True


def test_recipe_routes_food_costs(client, monkeypatch):
    """GET /api/inventory/food-costs retorna lista con food_cost por plato."""
    from app.services import database as db_mod

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {"username": "admin_test", "restaurant_name": "Rest", "branch_id": 1, "role": "owner"}

    async def mock_get_restaurant(request):
        return {"id": 1, "whatsapp_number": "+57300", "name": "Rest"}

    async def mock_food_costs(restaurant_id):
        return [{"dish_name": "Pizza", "food_cost": 12000,
                 "breakdown": [{"ingredient": "Queso", "line_cost": 12000}]}]

    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr("app.routes.inventory.get_current_restaurant", mock_get_restaurant)
    monkeypatch.setattr(db_mod, "db_get_food_costs", mock_food_costs)

    resp = client.get("/api/inventory/food-costs", headers={"Authorization": "Bearer fake-token"})
    assert resp.status_code == 200
    fc = resp.json()["food_costs"]
    assert len(fc) == 1
    assert fc[0]["dish_name"] == "Pizza"
    assert fc[0]["food_cost"] == 12000
