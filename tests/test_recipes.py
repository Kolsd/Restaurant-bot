"""
Tests for PHASE 4: Recipes.
Covers: db_upsert_dish_recipe, db_get_dish_recipe, db_get_food_costs,
       db_deduct_inventory_for_order (recipe path + legacy fallback).
Does not require a database or real credentials.
"""
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.services.tenant_context import tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_row(d: dict):
    """Creates an asyncpg Row-like object from a dict."""
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
async def test_upsert_dish_recipe_calls_delete_and_insert():
    """Upsert must delete the previous lines and insert again."""
    from app.services import database as db

    result_rows = [_make_row({
        "id": 1, "ingredient_id": 10, "quantity": 0.5,
        "ingredient_name": "Queso", "unit": "kg", "cost_per_unit": 20000,
        "line_cost": 10000
    })]

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=result_rows)
    # fetchval calls: set_config #1 (tenant), depleted count, set_config #2 (db_get_dish_recipe)
    mock_conn.fetchval = AsyncMock(side_effect=[None, 0, None])
    # transaction context manager
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with tenant_scope(1):
            result = await db.db_upsert_dish_recipe(
                restaurant_id=1,
                dish_name="Pizza",
                lines=[{"ingredient_id": 10, "quantity": 0.5}]
            )

    # DELETE was called
    delete_calls = [c for c in mock_conn.execute.call_args_list
                    if "DELETE" in str(c)]
    assert len(delete_calls) >= 1

    # INSERT was called
    insert_calls = [c for c in mock_conn.execute.call_args_list
                    if "INSERT" in str(c)]
    assert len(insert_calls) >= 1

    # Returns the lines from the subsequent GET
    assert len(result) == 1
    assert result[0]["ingredient_id"] == 10


@pytest.mark.asyncio
async def test_upsert_dish_recipe_empty_deletes_recipe():
    """Passing lines=[] must only run the DELETE (removing the recipe)."""
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_conn.fetch   = AsyncMock(return_value=[])
    # fetchval calls: set_config #1 (tenant), set_config #2 (db_get_dish_recipe)
    # No depleted check when lines=[]
    mock_conn.fetchval = AsyncMock(side_effect=[None, None])
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with tenant_scope(1):
            result = await db.db_upsert_dish_recipe(1, "Pizza", [])

    # Phase 5c: _sync_dish_availability_conn inserts into menu_availability when
    # lines=[] — only dish_recipes INSERTs must be absent (the recipe was deleted).
    recipe_insert_calls = [c for c in mock_conn.execute.call_args_list
                           if "INSERT INTO dish_recipes" in str(c)]
    assert len(recipe_insert_calls) == 0
    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 2. db_get_food_costs
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_food_costs_returns_list_with_breakdown():
    """db_get_food_costs must return dish_name, food_cost and breakdown."""
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
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with tenant_scope(1):
            result = await db.db_get_food_costs(restaurant_id=1)

    assert len(result) == 1
    assert result[0]["dish_name"] == "Pizza Margarita"
    assert float(result[0]["food_cost"]) == 4000
    assert isinstance(result[0]["breakdown"], list)


@pytest.mark.asyncio
async def test_get_food_costs_without_recipes_returns_empty_list():
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(db, "get_pool", AsyncMock(return_value=_make_pool(mock_conn))):
        with tenant_scope(99):
            result = await db.db_get_food_costs(restaurant_id=99)

    assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. db_deduct_inventory_for_order — recipe path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_deduct_uses_recipe_when_it_exists():
    """
    If there are lines in dish_recipes for a dish, it must deduct per ingredient
    (recipe_qty × ordered_qty) and NOT use linked_dishes.
    """
    from app.services import database as db
    import app.repositories.inventory_repo as inv_repo

    restaurant = {"id": 1}

    # recipe row: 0.3 kg of cheese per serving
    recipe_row = _make_row({"ingredient_id": 10, "recipe_qty": 0.3})
    # locked inventory row: 5 kg stock
    locked_row = _make_row({
        # recipe_ingredient_id is what the recipe points at; id is the row of
        # the sede actually cooking. Same value in a single-sede mock.
        "recipe_ingredient_id": 10, "id": 10, "current_stock": 5.0, "min_stock": 0.5,
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
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    # Phase 5c: stub out the new auto-hide helper so it doesn't consume conn.fetch
    async def _noop_sync(conn, ingredient_id, new_stock, min_stock, restaurant_id,
                         location_id=None):
        pass

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(inv_repo, "_sync_ingredient_dishes_conn", _noop_sync),
    ):
        with tenant_scope(1):
            await db.db_deduct_inventory_for_order(
                org_id=1, items=[{"name": "Pizza", "quantity": 2}]
            )

    # There must be an inventory UPDATE via fetchrow (uses RETURNING)
    update_calls = [c for c in mock_conn.fetchrow.call_args_list
                    if "UPDATE inventory" in str(c)]
    assert len(update_calls) == 1

    # deduct = recipe_qty * qty = 0.3 * 2 = 0.6
    # args: (sql, deduct, ing_id)
    update_args = update_calls[0].args
    assert abs(float(update_args[1]) - 0.6) < 0.001

    # History logged via execute
    history_calls = [c for c in mock_conn.execute.call_args_list
                     if "inventory_history" in str(c)]
    assert len(history_calls) == 1


@pytest.mark.asyncio
async def test_deduct_uses_linked_dishes_fallback_without_recipe():
    """
    If dish_recipes has no lines for the dish, it falls back to the
    legacy linked_dishes behavior.
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
        [],           # dish_recipes → empty → fallback
        [legacy_row], # linked_dishes FOR UPDATE
    ])
    # fetchrow returns updated stock after UPDATE ... RETURNING current_stock
    mock_conn.fetchrow = AsyncMock(return_value=MagicMock(__getitem__=lambda s, k: 7.0))
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
    ):
        with tenant_scope(1):
            await db.db_deduct_inventory_for_order(
                org_id=1,
                items=[{"name": "Hamburguesa", "quantity": 3}]
            )

    # UPDATE uses fetchrow (RETURNING current_stock)
    update_calls = [c for c in mock_conn.fetchrow.call_args_list
                    if "UPDATE inventory" in str(c)]
    assert len(update_calls) == 1
    # args: (sql, qty=3, row_id=5) — deduction amount is 3
    assert abs(float(update_calls[0].args[1]) - 3.0) < 0.001


@pytest.mark.asyncio
async def test_deduct_deactivates_dish_when_stock_runs_out():
    """
    When an ingredient's stock drops to ≤ min_stock, it must call
    _sync_dish_availability_conn to deactivate the linked dishes.
    """
    from app.services import database as db
    import app.repositories.inventory_repo as inv_repo

    restaurant = {"id": 1}

    recipe_row = _make_row({"ingredient_id": 7, "recipe_qty": 0.5})
    locked_row = _make_row({
        "recipe_ingredient_id": 7, "id": 7, "current_stock": 0.5, "min_stock": 0.5,
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
    mock_conn.fetchval = AsyncMock(return_value=None)
    mock_conn.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=False),
    ))

    sync_calls = []

    async def fake_sync_conn(conn, dish_names, available, restaurant_id, location_id=None):
        sync_calls.append((dish_names, available))

    # Phase 5c: stub out _sync_ingredient_dishes_conn so it doesn't consume
    # additional conn.fetch calls; the linked_dishes path is what this test
    # exercises via _sync_dish_availability_conn.
    async def _noop_ingredient_sync(conn, ingredient_id, new_stock, min_stock, restaurant_id,
                                    location_id=None):
        pass

    with (
        patch.object(db, "get_pool",
                     AsyncMock(return_value=_make_pool(mock_conn))),
        patch.object(inv_repo, "_sync_dish_availability_conn", fake_sync_conn),
        patch.object(inv_repo, "_sync_ingredient_dishes_conn", _noop_ingredient_sync),
    ):
        with tenant_scope(1):
            await db.db_deduct_inventory_for_order(
                org_id=1,
                items=[{"name": "Sopa del día", "quantity": 1}]
            )

    # _sync_dish_availability_conn must have been called with available=False
    assert len(sync_calls) == 1
    assert sync_calls[0][1] is False
    assert "Sopa del día" in sync_calls[0][0]


# ══════════════════════════════════════════════════════════════════════════════
# 4. HTTP routes — /api/inventory/recipes
# ══════════════════════════════════════════════════════════════════════════════

def test_recipe_routes_upsert_and_delete(client, monkeypatch):
    """POST and DELETE of /api/inventory/recipes return 200 with correct data."""
    from app.services import database as db_mod
    from app.routes.deps import get_current_restaurant_scoped
    from app.main import app

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {"username": "admin_test", "restaurant_name": "Rest", "branch_id": 1, "role": "owner"}

    async def mock_upsert(restaurant_id, dish_name, lines):
        return [{"ingredient_id": 10, "quantity": 0.5, "ingredient_name": "Queso",
                 "unit": "kg", "cost_per_unit": 20000, "line_cost": 10000}]

    async def mock_delete(restaurant_id, dish_name):
        pass

    async def mock_scoped_override():
        yield {"id": 1, "name": "Rest"}

    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr(db_mod, "db_upsert_dish_recipe", mock_upsert)
    monkeypatch.setattr(db_mod, "db_delete_dish_recipe", mock_delete)
    app.dependency_overrides[get_current_restaurant_scoped] = mock_scoped_override

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

    # Cleanup
    app.dependency_overrides.pop(get_current_restaurant_scoped, None)


def test_recipe_routes_food_costs(client, monkeypatch):
    """GET /api/inventory/food-costs returns a list with food_cost per dish."""
    from app.services import database as db_mod
    from app.routes.deps import get_current_restaurant_scoped
    from app.main import app

    async def mock_verify_token(token: str):
        return "admin_test"

    async def mock_get_user(username: str):
        return {"username": "admin_test", "restaurant_name": "Rest", "branch_id": 1, "role": "owner"}

    async def mock_food_costs(restaurant_id):
        return [{"dish_name": "Pizza", "food_cost": 12000,
                 "breakdown": [{"ingredient": "Queso", "line_cost": 12000}]}]

    async def mock_scoped_override():
        yield {"id": 1, "name": "Rest"}

    monkeypatch.setattr("app.routes.deps.verify_token", mock_verify_token)
    monkeypatch.setattr(db_mod, "db_get_user", mock_get_user)
    monkeypatch.setattr(db_mod, "db_get_food_costs", mock_food_costs)
    app.dependency_overrides[get_current_restaurant_scoped] = mock_scoped_override

    resp = client.get("/api/inventory/food-costs", headers={"Authorization": "Bearer fake-token"})
    assert resp.status_code == 200
    fc = resp.json()["food_costs"]
    assert len(fc) == 1
    assert fc[0]["dish_name"] == "Pizza"
    assert fc[0]["food_cost"] == 12000

    # Cleanup
    app.dependency_overrides.pop(get_current_restaurant_scoped, None)
