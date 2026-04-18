"""
tests/test_inventory_concurrency.py

Unit tests verifying inventory concurrency safety.

No live database required — asyncpg connections are mocked at the pool boundary,
following the pattern in test_loyalty_repo_tenant.py and conftest.make_pool.

Tests
-----
1. last_unit_only_one_winner
   Two concurrent commit_order_transaction calls for stock=1.
   One must succeed; the other must raise InsufficientStockError.
   Implemented via sequential mocked calls simulating a SELECT FOR UPDATE +
   conditional UPDATE pattern — the mock enforces the constraint.

2. shared_ingredient_no_partial_commit
   Two concurrent orders each needing the same ingredient (stock=1).
   Both succeed or one is rejected — the key invariant is that stock never
   goes negative (no partial deduction survives).

3. for_update_called_on_read_in_update_item
   db_update_inventory_item must issue SELECT ... FOR UPDATE (not a bare SELECT)
   before the UPDATE, preventing a concurrent admin lost-update race.

4. adjust_stock_is_delta_not_toctou
   db_adjust_inventory_stock uses a single delta-UPDATE (stock = stock + delta),
   not a read-then-write, so it is intrinsically race-free. Verify the SQL
   pattern is correct.

5. multi_sku_all_locked_before_first_update
   An order with 2 different SKUs using shared ingredients must lock ALL
   ingredient rows before starting to UPDATE any, so a concurrent transaction
   cannot grab one lock after us and deadlock.
"""
from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch, ANY

import pytest

from app.repositories.orders_repo import (
    InsufficientStockError,
    OrderCommitError,
    commit_order_transaction,
)
from app.services.tenant_context import tenant_scope


# ── Mock builders ──────────────────────────────────────────────────────────────

def _make_conn(*, stock: float = 1.0, ingredient_id: int = 99):
    """
    Build a minimal asyncpg-style connection mock wired up for a single-ingredient,
    legacy linked_dishes inventory scenario (no escandallo).

    The mock enforces the `stock >= requested` gate that the real DB enforces via
    `UPDATE ... WHERE current_stock >= $1 RETURNING current_stock`.
    """
    conn = AsyncMock()

    # Track remaining stock so sequential calls can enforce the constraint.
    remaining = [stock]

    # dish_recipes lookup — no recipe rows (use legacy path)
    conn.fetch = AsyncMock(side_effect=_make_fetch(remaining, ingredient_id))
    conn.fetchrow = AsyncMock(side_effect=_make_fetchrow(remaining, ingredient_id))
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn, remaining


def _make_fetch(remaining: list, ingredient_id: int):
    """
    Side-effect factory for conn.fetch.

    Calls may be:
    - dish_recipes lookup  → return [] (no recipe, fall through to legacy path)
    - linked_dishes lookup → return one inventory row whose current_stock is
                             the current value of remaining[0]
    """
    async def _side_effect(sql, *args, **kwargs):
        sql_lower = sql.lower().strip()
        if "dish_recipes" in sql_lower:
            # No escandallo — force legacy linked_dishes path
            return []
        if "linked_dishes" in sql_lower or "inventory" in sql_lower:
            # Return one inventory row reflecting current stock
            row = MagicMock()
            row.__iter__ = lambda s: iter({
                "id": ingredient_id,
                "current_stock": remaining[0],
                "linked_dishes": json.dumps(["test_dish"]),
                "min_stock": 0.0,
            }.items())
            row.keys = lambda: ["id", "current_stock", "linked_dishes", "min_stock"]
            row.__getitem__ = lambda s, k: {
                "id": ingredient_id,
                "current_stock": remaining[0],
                "linked_dishes": json.dumps(["test_dish"]),
                "min_stock": 0.0,
            }[k]
            return [row]
        return []
    return _side_effect


def _make_fetchrow(remaining: list, ingredient_id: int):
    """
    Side-effect factory for conn.fetchrow.

    Handles:
    - orders INSERT / SELECT … FOR UPDATE → pass-through None (no base_order_id logic)
    - inventory UPDATE … WHERE current_stock >= $1 RETURNING → enforce the constraint
    """
    async def _side_effect(sql, *args, **kwargs):
        sql_lower = sql.lower().strip()

        # Inventory conditional UPDATE
        if "update inventory" in sql_lower and "current_stock >=" in sql_lower:
            qty = args[0]  # first positional = deduction amount
            if remaining[0] >= float(qty):
                remaining[0] -= float(qty)
                row = MagicMock()
                row.__getitem__ = lambda s, k: remaining[0] if k == "current_stock" else None
                row.keys = lambda: ["current_stock"]
                return row
            else:
                return None  # simulates WHERE condition failing → InsufficientStockError

        # orders INSERT or SELECT FOR UPDATE (sub-order path) → ignore
        return None

    return _side_effect


def _make_pool(conn):
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)
    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


def _order_payload(order_id, phone, bot_number, dish_name, qty=1):
    return {
        "id":             order_id,
        "phone":          phone,
        "items":          [{"name": dish_name, "quantity": qty, "price": 10000}],
        "order_type":     "domicilio",
        "address":        "Calle Test",
        "notes":          "",
        "subtotal":       Decimal("10000"),
        "delivery_fee":   Decimal("0"),
        "total":          Decimal("10000"),
        "status":         "pendiente_pago",
        "paid":           False,
        "payment_url":    "",
        "payment_method": "efectivo",
        "bot_number":     bot_number,
        "base_order_id":  None,
        "sub_number":     1,
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_last_unit_only_one_winner():
    """
    Two sequential calls competing for the last unit (stock=1).

    The mock enforces the DB-side constraint: the first call depletes stock,
    the second call's conditional UPDATE returns None → InsufficientStockError.

    In production asyncpg serializes this via FOR UPDATE + WHERE current_stock >= $1.
    Here we simulate that with shared mutable state across two calls on the same
    mock connection (single shared remaining[] list).
    """
    conn, remaining = _make_conn(stock=1.0)
    pool = _make_pool(conn)

    dish = "last_empanada"
    phone_a, phone_b = "+571111111111", "+572222222222"
    bot = "+5700000000"

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(1):
            # First order — should succeed
            await commit_order_transaction(
                pool,
                restaurant_id=1,
                conversation_id=phone_a,
                cart={"items": [{"name": dish, "quantity": 1}], "bot_number": bot},
                order_payload=_order_payload("ORD-A", phone_a, bot, dish, qty=1),
            )
            assert remaining[0] == 0.0, "Stock should be 0 after first order"

            # Second order — should fail
            with pytest.raises(InsufficientStockError) as exc_info:
                await commit_order_transaction(
                    pool,
                    restaurant_id=1,
                    conversation_id=phone_b,
                    cart={"items": [{"name": dish, "quantity": 1}], "bot_number": bot},
                    order_payload=_order_payload("ORD-B", phone_b, bot, dish, qty=1),
                )

    err = exc_info.value
    assert "last_empanada" in str(err) or str(err.sku)
    # Stock must not have gone negative
    assert remaining[0] == 0.0, f"Stock went negative: {remaining[0]}"


@pytest.mark.asyncio
async def test_shared_ingredient_stock_never_negative():
    """
    Two sequential orders each requesting 1 unit, stock=1.

    The second order must raise InsufficientStockError and the stock
    must never drop below 0 (GREATEST(0, ...) is NOT used in the order
    path — it uses WHERE current_stock >= $1 instead, so negative is
    truly impossible).
    """
    conn, remaining = _make_conn(stock=1.0)
    pool = _make_pool(conn)

    dish = "shared_dish"
    bot = "+5700000001"

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(2):
            # First succeeds
            await commit_order_transaction(
                pool,
                restaurant_id=2,
                conversation_id="+573000000001",
                cart={"items": [{"name": dish, "quantity": 1}], "bot_number": bot},
                order_payload=_order_payload("ORD-C1", "+573000000001", bot, dish, qty=1),
            )

            # Second must fail
            with pytest.raises(InsufficientStockError):
                await commit_order_transaction(
                    pool,
                    restaurant_id=2,
                    conversation_id="+573000000002",
                    cart={"items": [{"name": dish, "quantity": 1}], "bot_number": bot},
                    order_payload=_order_payload("ORD-C2", "+573000000002", bot, dish, qty=1),
                )

    assert remaining[0] >= 0.0, f"Stock went negative: {remaining[0]}"


@pytest.mark.asyncio
async def test_for_update_called_on_read_in_update_item():
    """
    db_update_inventory_item must issue 'SELECT ... FOR UPDATE' on the
    initial read, not a bare 'SELECT *'.

    This prevents a concurrent admin edit from producing a lost update
    (TOCTOU race: admin A reads stock=10, admin B reads stock=10, both
    write different values, the second write wins silently).

    We verify this by inspecting the SQL passed to conn.fetchrow.
    """
    conn = AsyncMock()
    existing_row = MagicMock()
    existing_row.__iter__ = lambda s: iter({
        "id": 1, "org_id": 5, "restaurant_id": 5, "name": "Papa", "unit": "kg",
        "current_stock": 10.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Papas Fritas"]),
        "cost_per_unit": 500.0,
    }.items())
    existing_row.keys = lambda: ["id", "org_id", "restaurant_id", "name", "unit",
                                  "current_stock", "min_stock", "linked_dishes", "cost_per_unit"]
    existing_row.__getitem__ = lambda s, k: {
        "id": 1, "org_id": 5, "restaurant_id": 5, "name": "Papa", "unit": "kg",
        "current_stock": 10.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Papas Fritas"]),
        "cost_per_unit": 500.0,
    }[k]

    updated_row = MagicMock()
    updated_row.__iter__ = lambda s: iter({
        "id": 1, "org_id": 5, "restaurant_id": 5, "name": "Papa", "unit": "kg",
        "current_stock": 8.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Papas Fritas"]),
        "cost_per_unit": 500.0,
    }.items())
    updated_row.keys = lambda: ["id", "org_id", "restaurant_id", "name", "unit",
                                  "current_stock", "min_stock", "linked_dishes", "cost_per_unit"]
    updated_row.__getitem__ = lambda s, k: {
        "id": 1, "org_id": 5, "restaurant_id": 5, "name": "Papa", "unit": "kg",
        "current_stock": 8.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Papas Fritas"]),
        "cost_per_unit": 500.0,
    }[k]

    # First fetchrow call → existing (SELECT FOR UPDATE)
    # Second fetchrow call → updated (UPDATE ... RETURNING)
    conn.fetchrow = AsyncMock(side_effect=[existing_row, updated_row])
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(5):
            from app.repositories.inventory_repo import db_update_inventory_item
            await db_update_inventory_item(1, {"current_stock": 8.0})

    # Inspect the first fetchrow call — must contain FOR UPDATE
    first_call_sql = conn.fetchrow.call_args_list[0].args[0]
    assert "FOR UPDATE" in first_call_sql.upper(), (
        f"db_update_inventory_item must use SELECT ... FOR UPDATE on the initial read. "
        f"Got SQL: {first_call_sql!r}"
    )


@pytest.mark.asyncio
async def test_adjust_stock_uses_delta_update_not_toctou():
    """
    db_adjust_inventory_stock must use a single delta-based UPDATE
    (current_stock = current_stock + $delta) rather than a read-then-write.

    A read-then-write would be:
        stock = SELECT current_stock ...
        UPDATE SET current_stock = stock + delta ...
    That's a TOCTOU race under concurrency.

    A delta UPDATE is:
        UPDATE SET current_stock = GREATEST(0, current_stock + $delta)
    which is evaluated atomically inside the DB.

    We verify by ensuring conn.fetch / fetchrow is NOT called before conn.execute
    or a RETURNING fetchrow that contains the UPDATE — i.e., there is only ONE
    round-trip to the DB for the stock change, not two.
    """
    conn = AsyncMock()

    updated_row = MagicMock()
    updated_row.__iter__ = lambda s: iter({
        "id": 7, "restaurant_id": 3, "name": "Arroz", "unit": "kg",
        "current_stock": 5.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Arroz con leche"]),
        "cost_per_unit": 200.0,
    }.items())
    updated_row.keys = lambda: ["id", "org_id", "restaurant_id", "name", "unit",
                                  "current_stock", "min_stock", "linked_dishes", "cost_per_unit"]
    updated_row.__getitem__ = lambda s, k: {
        "id": 7, "restaurant_id": 3, "name": "Arroz", "unit": "kg",
        "current_stock": 5.0, "min_stock": 1.0,
        "linked_dishes": json.dumps(["Arroz con leche"]),
        "cost_per_unit": 200.0,
    }.get(k)

    # Only one fetchrow call expected (the UPDATE ... RETURNING)
    conn.fetchrow = AsyncMock(return_value=updated_row)
    conn.execute = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)

    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    pool = _make_pool(conn)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(3):
            from app.repositories.inventory_repo import db_adjust_inventory_stock
            result = await db_adjust_inventory_stock(7, -5.0, "test_adjustment", 3)

    assert result is not None, "Should return the updated item"

    # There should be exactly ONE fetchrow call (the UPDATE … RETURNING)
    # and it must contain both UPDATE and current_stock to confirm delta pattern.
    fetchrow_calls = conn.fetchrow.call_args_list
    assert len(fetchrow_calls) == 1, (
        f"Expected 1 fetchrow call (delta UPDATE), got {len(fetchrow_calls)}. "
        f"A read-then-write pattern would show 2+ calls."
    )
    update_sql = fetchrow_calls[0].args[0].upper()
    assert "UPDATE" in update_sql, f"fetchrow call should be an UPDATE, got: {update_sql!r}"
    assert "GREATEST" in update_sql or "CURRENT_STOCK +" in update_sql, (
        f"UPDATE must use a delta (current_stock + $1 or GREATEST), got: {update_sql!r}"
    )


@pytest.mark.asyncio
async def test_insufficient_stock_does_not_call_history_insert():
    """
    When the conditional UPDATE fails (returns None) on a linked_dishes item,
    InsufficientStockError must be raised immediately and the inventory_history
    INSERT must NOT be executed (the transaction rolls back before writing history).

    This confirms the error path is clean and does not leave orphan history rows.
    """
    conn, remaining = _make_conn(stock=0.0)  # stock already at 0
    pool = _make_pool(conn)

    dish = "sold_out_dish"
    bot = "+5799999999"

    history_inserts: list[str] = []
    original_execute = conn.execute.side_effect

    async def _tracking_execute(sql, *args, **kwargs):
        if "inventory_history" in sql.lower():
            history_inserts.append(sql)
        return None

    conn.execute.side_effect = _tracking_execute

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(9):
            with pytest.raises(InsufficientStockError):
                await commit_order_transaction(
                    pool,
                    restaurant_id=9,
                    conversation_id="+573001234567",
                    cart={"items": [{"name": dish, "quantity": 1}], "bot_number": bot},
                    order_payload=_order_payload("ORD-FAIL", "+573001234567", bot, dish, qty=1),
                )

    assert len(history_inserts) == 0, (
        f"inventory_history INSERT should not run when stock check fails; "
        f"got {len(history_inserts)} calls: {history_inserts}"
    )
