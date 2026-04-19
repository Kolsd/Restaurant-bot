"""
Integration tests for commit_order_transaction.

Uses a REAL PostgreSQL database (TEST_DATABASE_URL or DATABASE_URL).
Every test runs inside a transaction that is rolled back at teardown,
so the DB is left in exactly the state it was before the suite ran.

Fixtures are defined in conftest.py:
  db_pool  — asyncpg Pool (skips if no URL)
  db_conn  — single connection wrapped in a savepoint-rolled-back transaction

Wave-2 changes (post-0037/0038):
  • restaurants is a READ-ONLY VIEW — INSERT goes to organizations + locations.
  • inventory / carts / orders use org_id (restaurant_id dropped).
  • commit_order_transaction uses _tenant_connection() (not the pool arg) so
    tests must patch app.services.database.get_pool AND wrap in tenant_scope().
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import asyncpg

from app.repositories.orders_repo import (
    commit_order_transaction,
    InsufficientStockError,
    OrderCommitError,
)
from app.services.tenant_context import tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


async def _insert_restaurant(conn) -> int:
    """
    Insert a minimal org + primary location and return the org_id.

    Wave-2: restaurants is a VIEW where id = location_id.
    We insert the location with explicit id = org_id so that:
      - `restaurants WHERE id = org_id` VIEW lookup resolves correctly
      - commit_order_transaction inserts orders with org_id = org_id, and RLS
        passes because tenant_scope(org_id) sets app.org_id = org_id
      - inventory/carts are inserted with org_id = org_id, consistent with scope

    locations.id is a plain SERIAL — explicit id inserts are allowed.
    """
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number)
           VALUES ($1, $2)
           RETURNING id""",
        "Restaurante Test " + _uid()[:8],
        "+57" + str(uuid.uuid4().int)[:10],
    )
    # Force location_id = org_id (see docstring)
    await conn.execute(
        """INSERT INTO locations (id, org_id, name, code, address, active, timezone)
           VALUES ($1, $1, 'Test Location', 'main', NULL, true, 'America/Bogota')""",
        org_id,
    )
    # Advance locations sequence past org_id to avoid future auto-id collisions.
    await conn.execute(
        "SELECT setval('locations_id_seq', GREATEST(nextval('locations_id_seq'), $1))",
        org_id,
    )
    return org_id


async def _insert_inventory_item(
    conn,
    restaurant_id: int,
    *,
    name: str,
    stock: float,
    min_stock: float = 0.0,
) -> int:
    """Insert an inventory item (no escandallo) linked to `name` as a dish.

    Wave-2: inventory uses org_id (not restaurant_id).
    """
    row = await conn.fetchrow(
        """INSERT INTO inventory
               (org_id, name, unit, current_stock, min_stock, linked_dishes)
           VALUES ($1, $2, 'unidades', $3, $4, $5::jsonb)
           RETURNING id""",
        restaurant_id,
        name,
        stock,
        min_stock,
        json.dumps([name]),
    )
    return row["id"]


async def _insert_cart(conn, phone: str, bot_number: str, restaurant_id: int, items: list[dict]) -> None:
    """Upsert a cart row.

    Wave-2: carts has org_id NOT NULL — must be provided.
    """
    await conn.execute(
        """INSERT INTO carts (phone, bot_number, org_id, cart_data)
           VALUES ($1, $2, $3, $4::jsonb)
           ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data = EXCLUDED.cart_data""",
        phone,
        bot_number,
        restaurant_id,
        json.dumps({"items": items, "order_type": "domicilio", "address": "Calle 1", "notes": ""}),
    )


def _make_order_payload(
    order_id: str,
    phone: str,
    bot_number: str,
    items: list[dict],
    *,
    subtotal=Decimal("35000"),
    delivery_fee=Decimal("3500"),
    total=Decimal("38500"),
) -> dict:
    return {
        "id":             order_id,
        "phone":          phone,
        "items":          items,
        "order_type":     "domicilio",
        "address":        "Calle 1 # 2-3",
        "notes":          "",
        "subtotal":       subtotal,
        "delivery_fee":   delivery_fee,
        "total":          total,
        "status":         "pendiente_pago",
        "paid":           False,
        "payment_url":    "",
        "payment_method": "wompi",
        "bot_number":     bot_number,
        "base_order_id":  None,
        "sub_number":     1,
    }


def _make_pool_for_conn(conn):
    """Return a fake pool whose .acquire() yields `conn` without touching it."""

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = AsyncMock()
    pool.acquire = _acquire
    return pool


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_happy_path_order_committed(db_conn):
    """
    Happy path: order is inserted, stock is decremented, cart is deleted.

    Inventory: hamburguesa stock=10
    Cart:      2x hamburguesa
    Expected:  order row exists, stock=8, cart gone.
    """
    restaurant_id = await _insert_restaurant(db_conn)
    dish_name = "hamburguesa_" + _uid()[:6]

    inv_id = await _insert_inventory_item(
        db_conn, restaurant_id, name=dish_name, stock=10
    )

    phone = "+57300" + str(uuid.uuid4().int)[:7]
    bot_number = "+57900" + str(uuid.uuid4().int)[:7]
    order_id = "ORD-" + _uid()

    items = [{"name": dish_name, "quantity": 2, "price": 17500}]
    await _insert_cart(db_conn, phone, bot_number, restaurant_id, items)

    pool = _make_pool_for_conn(db_conn)
    payload = _make_order_payload(order_id, phone, bot_number, items)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(restaurant_id):
            await commit_order_transaction(
                pool,
                restaurant_id=restaurant_id,
                conversation_id=phone,
                cart={"items": items, "bot_number": bot_number},
                order_payload=payload,
            )

    # Order row must exist
    order = await db_conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
    assert order is not None, "Order row was not inserted"
    assert order["phone"] == phone
    assert order["status"] == "pendiente_pago"

    # Stock must have been decremented by 2
    inv = await db_conn.fetchrow("SELECT current_stock FROM inventory WHERE id = $1", inv_id)
    assert float(inv["current_stock"]) == 8.0, f"Expected stock=8, got {inv['current_stock']}"

    # Cart must be deleted
    cart = await db_conn.fetchrow(
        "SELECT 1 FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number
    )
    assert cart is None, "Cart was not deleted after successful commit"


@pytest.mark.asyncio
async def test_insufficient_stock_raises_and_rolls_back(db_conn):
    """
    Ordering more than available stock raises InsufficientStockError.
    The order must NOT be created and stock must stay at its original value.

    Inventory: item stock=1
    Cart:      3x of that item
    """
    restaurant_id = await _insert_restaurant(db_conn)
    dish_name = "empanada_" + _uid()[:6]

    inv_id = await _insert_inventory_item(
        db_conn, restaurant_id, name=dish_name, stock=1
    )

    phone = "+57300" + str(uuid.uuid4().int)[:7]
    bot_number = "+57900" + str(uuid.uuid4().int)[:7]
    order_id = "ORD-" + _uid()

    items = [{"name": dish_name, "quantity": 3, "price": 5000}]
    await _insert_cart(db_conn, phone, bot_number, restaurant_id, items)

    pool = _make_pool_for_conn(db_conn)
    payload = _make_order_payload(order_id, phone, bot_number, items, subtotal=Decimal("15000"), total=Decimal("15000"))

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(restaurant_id):
            with pytest.raises(InsufficientStockError) as exc_info:
                await commit_order_transaction(
                    pool,
                    restaurant_id=restaurant_id,
                    conversation_id=phone,
                    cart={"items": items, "bot_number": bot_number},
                    order_payload=payload,
                )

    err = exc_info.value
    assert err.requested == 3.0
    assert err.available == 1.0

    # Order must NOT have been created
    order = await db_conn.fetchrow("SELECT 1 FROM orders WHERE id = $1", order_id)
    assert order is None, "Order should not exist after InsufficientStockError"

    # Stock must be unchanged
    inv = await db_conn.fetchrow("SELECT current_stock FROM inventory WHERE id = $1", inv_id)
    assert float(inv["current_stock"]) == 1.0, (
        f"Stock was modified despite rollback: {inv['current_stock']}"
    )


@pytest.mark.asyncio
async def test_decimal_coercion_float_inputs(db_conn):
    """
    When order_payload contains float or Decimal values for subtotal / total /
    delivery_fee, commit_order_transaction coerces them to Decimal before
    binding to asyncpg.

    The ``orders`` table stores these columns as INTEGER, so only whole-number
    values can round-trip through the DB.  The important contract being tested
    is that the function does NOT crash when floats are supplied and that the
    values are stored correctly (``to_decimal`` converts and asyncpg casts to
    INTEGER on the way in).
    """
    restaurant_id = await _insert_restaurant(db_conn)

    phone = "+57300" + str(uuid.uuid4().int)[:7]
    bot_number = "+57900" + str(uuid.uuid4().int)[:7]
    order_id = "ORD-" + _uid()

    # No items → no inventory deduction needed
    items = []
    await _insert_cart(db_conn, phone, bot_number, restaurant_id, items)

    # Pass float values — commit_order_transaction must coerce without crashing.
    # Use whole-number floats so they round-trip through the INTEGER column.
    payload = _make_order_payload(
        order_id, phone, bot_number, items,
        subtotal=35000.0,      # float, whole number
        delivery_fee=3000.0,   # float, whole number
        total=38000.0,         # float, whole number
    )

    pool = _make_pool_for_conn(db_conn)
    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(restaurant_id):
            await commit_order_transaction(
                pool,
                restaurant_id=restaurant_id,
                conversation_id=phone,
                cart={"items": items, "bot_number": bot_number},
                order_payload=payload,
            )

    row = await db_conn.fetchrow(
        "SELECT subtotal, delivery_fee, total FROM orders WHERE id = $1", order_id
    )
    assert row is not None, "Order row not found"

    # The orders schema stores these as INTEGER — asyncpg returns int.
    assert isinstance(row["subtotal"], int), (
        f"subtotal should be int (INTEGER column), got {type(row['subtotal'])}"
    )
    assert row["subtotal"] == 35000, f"Unexpected subtotal: {row['subtotal']}"
    assert row["delivery_fee"] == 3000, f"Unexpected delivery_fee: {row['delivery_fee']}"
    assert row["total"] == 38000, f"Unexpected total: {row['total']}"


@pytest.mark.asyncio
async def test_cart_deleted_on_success(db_conn):
    """
    After a successful commit, the cart row for (phone, bot_number) is gone.
    Verifies isolation: a second cart for a different phone is untouched.
    """
    restaurant_id = await _insert_restaurant(db_conn)

    phone = "+57300" + str(uuid.uuid4().int)[:7]
    other_phone = "+57301" + str(uuid.uuid4().int)[:7]
    bot_number = "+57900" + str(uuid.uuid4().int)[:7]
    order_id = "ORD-" + _uid()

    items = []
    await _insert_cart(db_conn, phone, bot_number, restaurant_id, items)
    await _insert_cart(db_conn, other_phone, bot_number, restaurant_id, [{"name": "pizza", "quantity": 1}])

    pool = _make_pool_for_conn(db_conn)
    payload = _make_order_payload(order_id, phone, bot_number, items)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(restaurant_id):
            await commit_order_transaction(
                pool,
                restaurant_id=restaurant_id,
                conversation_id=phone,
                cart={"items": items, "bot_number": bot_number},
                order_payload=payload,
            )

    # Target cart deleted
    deleted = await db_conn.fetchrow(
        "SELECT 1 FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number
    )
    assert deleted is None, "Target cart was not deleted"

    # Bystander cart untouched
    bystander = await db_conn.fetchrow(
        "SELECT 1 FROM carts WHERE phone=$1 AND bot_number=$2", other_phone, bot_number
    )
    assert bystander is not None, "Bystander cart was incorrectly deleted"


@pytest.mark.asyncio
async def test_cart_not_deleted_on_insufficient_stock(db_conn):
    """
    When InsufficientStockError is raised, the cart must survive.
    The caller needs the cart intact to show the user what failed.
    """
    restaurant_id = await _insert_restaurant(db_conn)
    dish_name = "tamal_" + _uid()[:6]

    await _insert_inventory_item(db_conn, restaurant_id, name=dish_name, stock=0)

    phone = "+57300" + str(uuid.uuid4().int)[:7]
    bot_number = "+57900" + str(uuid.uuid4().int)[:7]
    order_id = "ORD-" + _uid()

    items = [{"name": dish_name, "quantity": 1, "price": 12000}]
    await _insert_cart(db_conn, phone, bot_number, restaurant_id, items)

    pool = _make_pool_for_conn(db_conn)
    payload = _make_order_payload(order_id, phone, bot_number, items)

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        with tenant_scope(restaurant_id):
            with pytest.raises(InsufficientStockError):
                await commit_order_transaction(
                    pool,
                    restaurant_id=restaurant_id,
                    conversation_id=phone,
                    cart={"items": items, "bot_number": bot_number},
                    order_payload=payload,
                )

    # Cart must still exist
    cart = await db_conn.fetchrow(
        "SELECT cart_data FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number
    )
    assert cart is not None, "Cart should not have been deleted on rollback"
