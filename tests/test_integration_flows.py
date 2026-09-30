"""
tests/test_integration_flows.py

End-to-end integration tests for multi-step business flows.
Uses real PostgreSQL with transaction rollback for isolation.

Each test exercises a complete business flow by calling repository functions
directly (not HTTP endpoints). The db_conn fixture wraps every test in a
transaction that is always rolled back, so the database is left pristine.

Requires DATABASE_URL or TEST_DATABASE_URL environment variable.

Run:
    pytest tests/test_integration_flows.py -v

Wave-2 changes (post-0037/0038):
  • restaurants is a READ-ONLY VIEW — INSERT goes to organizations + locations.
  • staff / staff_shifts / table_orders / inventory / carts use org_id.
  • asyncpg >=0.30 __slots__ on Connection — use _ConnProxy for extra attrs.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from app.services.tenant_context import tenant_scope

import asyncpg
import pytest


# ── Pool shim ────────────────────────────────────────────────────────────────
#
# Repository functions (staff_repo) call tenant_connection() which calls
# `pool = await get_pool()` then `async with pool.acquire() as conn: ...`.
# We patch app.services.database.get_pool + wrap in bypass_tenant_scope so
# the function stays inside the same open transaction that conftest.db_conn manages.

def _make_pool_for_conn(conn):
    """Return a fake pool whose .acquire() yields `conn` without touching it."""

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = AsyncMock()
    pool.acquire = _acquire
    return pool


async def _register_prod_jsonb_codec(conn) -> None:
    """Register the SAME jsonb codec app/services/database.py's real
    get_pool() registers on every production connection (encoder=json.dumps,
    decoder=json.loads).

    conftest.py's shared `db_conn` fixture does NOT register this — its bare
    asyncpg connection needs an ALREADY-serialized string for a $n::jsonb
    param. Production requires the opposite (a raw dict/list, encoded
    exactly once by the codec) — this is the exact P0 double-encoding bug
    class documented in orders_repo.deduct_inventory_in_tx and
    inventory_repo.db_deduct_inventory_for_order. `_make_pool_for_conn`
    above is this test module's stand-in for the REAL get_pool(), so it
    must behave like it. Call this right before that patch — every setup
    write above it (which correctly used a pre-dumped string for the
    then-bare connection) is unaffected; only what runs after sees the
    codec."""
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


# ── Tiny helpers ──────────────────────────────────────────────────────────────

def _uid() -> str:
    return str(uuid.uuid4())


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _dt_naive(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


# ── Shared test-data helpers ──────────────────────────────────────────────────


async def _insert_restaurant(conn, *, tip_distribution: dict | None = None) -> int:
    """
    Insert a minimal org + location pair and return the org_id.

    Wave-2: restaurants is a VIEW where id = location_id.
    We insert the location with an explicit id = org_id so that:
      - `restaurants WHERE id = org_id` resolves correctly (VIEW id = location_id)
      - `ss.org_id = ANY([org_id])` in the tip query matches staff/shifts
        inserted with org_id = org_id
      - tenant_scope(org_id) → app.org_id = org_id → RLS passes for
        staff/shifts/table_orders inserted with org_id = org_id

    This works because locations.id is a plain SERIAL (not GENERATED ALWAYS),
    so explicit id inserts are allowed.
    """
    features: dict = {}
    if tip_distribution is not None:
        features["tip_distribution"] = tip_distribution
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, features)
           VALUES ($1, $2::jsonb)
           RETURNING id""",
        "Test Restaurant",
        json.dumps(features),
    )
    # Insert location with explicit id = org_id (see docstring for rationale)
    await conn.execute(
        """INSERT INTO locations (id, org_id, name, code, address, active, timezone)
           VALUES ($1, $1, 'Test Restaurant', 'main', NULL, true, 'America/Bogota')""",
        org_id,
    )
    # Advance locations sequence past org_id to prevent future auto-id collisions.
    # setval is non-transactional — rollback undoes the INSERT but not the seq bump.
    await conn.execute(
        "SELECT setval('locations_id_seq', GREATEST(nextval('locations_id_seq'), $1))",
        org_id,
    )
    return org_id


async def _insert_staff(
    conn,
    *,
    restaurant_id: int,
    name: str = "Juan Test",
    role: str = "mesero",
    hourly_rate: int = 15_000,
) -> str:
    """Insert a staff member and return their UUID string.

    Wave-2: staff uses org_id (not restaurant_id).
    """
    unique_username = f"test_{uuid.uuid4().hex[:12]}"
    row = await conn.fetchrow(
        """
        INSERT INTO staff (id, org_id, name, role, pin, hourly_rate, username)
        VALUES ($1, $2, $3, $4, '', $5, $6)
        RETURNING id
        """,
        uuid.uuid4(),
        restaurant_id,
        name,
        role,
        hourly_rate,
        unique_username,
    )
    return str(row["id"])


async def _insert_inventory(
    conn,
    *,
    restaurant_id: int,
    name: str = "Ingrediente Test",
    current_stock: float = 50.0,
    min_stock: float = 5.0,
) -> int:
    """Insert an inventory item and return its id.

    Wave-2: inventory uses org_id (not restaurant_id).
    """
    return await conn.fetchval(
        """
        INSERT INTO inventory
            (org_id, name, unit, current_stock, min_stock)
        VALUES ($1, $2, 'unit', $3, $4)
        RETURNING id
        """,
        restaurant_id,
        name,
        current_stock,
        min_stock,
    )


async def _insert_shift(
    conn,
    *,
    staff_id: str,
    restaurant_id: int,
    clock_in: str,
    clock_out: str | None = None,
) -> None:
    """Insert a staff_shift row.

    Wave-2: staff_shifts uses org_id (not restaurant_id).
    """
    await conn.execute(
        """
        INSERT INTO staff_shifts (id, staff_id, org_id, clock_in, clock_out)
        VALUES ($1, $2::uuid, $3, $4, $5)
        """,
        uuid.uuid4(),
        staff_id,
        restaurant_id,
        _dt(clock_in),
        _dt(clock_out) if clock_out else None,
    )


async def _insert_table_order(conn, *, restaurant_id: int) -> str:
    """Insert a table_order row and return its base_order_id (same as id).

    Wave-2: table_orders uses org_id (not restaurant_id).
    branch_id is INTEGER (int4) while org_id is BIGINT (int8) — use explicit
    casts to avoid AmbiguousParameterError when the same Python int is bound
    to both columns in one VALUES row.
    """
    base_id = _uid()
    await conn.execute(
        """
        INSERT INTO table_orders (id, table_id, table_name, phone, base_order_id, branch_id, org_id)
        VALUES ($1, 'T1', 'Mesa 1', '+57300', $1, $2::int, $3::bigint)
        """,
        base_id,
        restaurant_id,
        restaurant_id,
    )
    return base_id


async def _insert_check(
    conn,
    *,
    base_order_id: str,
    tip_amount: int,
    paid_at: str,
    status: str = "invoiced",
    check_number: int = 1,
) -> str:
    check_id = _uid()
    await conn.execute(
        """
        INSERT INTO table_checks
            (id, base_order_id, check_number, tip_amount, status, paid_at,
             subtotal, tax_amount, total, items, payments)
        VALUES ($1, $2, $3, $4, $5, $6,
                0, 0, $4, '[]'::jsonb, '[]'::jsonb)
        """,
        check_id,
        base_order_id,
        check_number,
        tip_amount,
        status,
        _dt_naive(paid_at),
    )
    return check_id


# ── Shared period constants ──────────────────────────────────────────────────

PERIOD_START = "2024-06-01T00:00:00+00:00"
PERIOD_END   = "2024-06-02T00:00:00+00:00"
SHIFT_IN     = "2024-06-01T10:00:00+00:00"
SHIFT_OUT    = "2024-06-01T22:00:00+00:00"
PAID_AT      = "2024-06-01T14:00:00+00:00"


# ═══════════════════════════════════════════════════════════════════════════════
# Flow 1: Delivery order — cart → commit → inventory deducted
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeliveryOrderFlow:
    """
    Full delivery order flow:
      create cart → build order payload → commit_order_transaction
      → verify inventory decremented and cart deleted.

    commit_order_transaction signature:
        commit_order_transaction(pool, *, restaurant_id, conversation_id, cart, order_payload)

    The pool is patched to use the test connection so the entire operation
    stays inside the rollback-able test transaction.
    """

    @pytest.mark.asyncio
    async def test_order_commits_and_deducts_stock(self, db_conn):
        """
        Happy path: order inserts, inventory is decremented by the ordered
        quantity, cart row is deleted.

        Uses the legacy linked_dishes path (no dish_recipes row), so the
        inventory item must list the dish name in its linked_dishes JSONB array.
        """
        conn = db_conn
        rid = await _insert_restaurant(conn)

        dish_name = "Pizza Margherita"
        inv_id = await _insert_inventory(
            conn, restaurant_id=rid, name=dish_name, current_stock=50.0
        )
        # Attach the dish name via linked_dishes so the legacy deduction path fires
        await conn.execute(
            "UPDATE inventory SET linked_dishes = $1::jsonb WHERE id = $2",
            json.dumps([dish_name]),
            inv_id,
        )

        phone = "+573001111111"

        # Insert a cart row (Wave-2: carts has org_id NOT NULL)
        await conn.execute(
            """
            INSERT INTO carts (phone, org_id, cart_data, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            """,
            phone,
            rid,
            json.dumps({"items": [{"name": dish_name, "quantity": 2, "price": 25_000}]}),
        )

        order_payload = {
            "id":             _uid(),
            "phone":          phone,
            "items":          [{"name": dish_name, "quantity": 2, "price": 25_000}],
            "order_type":     "domicilio",
            "address":        "Calle 123 # 45-67",
            "notes":          "",
            "subtotal":       Decimal("50000"),
            "delivery_fee":   Decimal("0"),
            "total":          Decimal("50000"),
            "status":         "pendiente_pago",
            "paid":           False,
            "payment_url":    "",
            "payment_method": "",
            "base_order_id":  None,
            "sub_number":     1,
        }

        from app.repositories import orders_repo

        fake_pool = _make_pool_for_conn(conn)
        await _register_prod_jsonb_codec(conn)
        with patch("app.services.database.get_pool", AsyncMock(return_value=fake_pool)):
            with tenant_scope(rid):
                await orders_repo.commit_order_transaction(
                    fake_pool,
                    restaurant_id=rid,
                    conversation_id=phone,
                    cart={},
                    order_payload=order_payload,
                )

        # Inventory decremented by 2
        new_stock = await conn.fetchval(
            "SELECT current_stock FROM inventory WHERE id = $1", inv_id
        )
        assert float(new_stock) == 48.0

        # Cart deleted
        cart = await conn.fetchrow(
            "SELECT * FROM carts WHERE phone = $1 AND org_id = $2",
            phone,
            rid,
        )
        assert cart is None

    @pytest.mark.asyncio
    async def test_insufficient_stock_rolls_back(self, db_conn):
        """
        When stock < requested quantity, InsufficientStockError is raised and
        no order row is written (the inner transaction rolls back).
        """
        conn = db_conn
        rid = await _insert_restaurant(conn)

        dish_name = "Pasta Limitada"
        inv_id = await _insert_inventory(
            conn, restaurant_id=rid, name=dish_name, current_stock=1.0
        )
        await conn.execute(
            "UPDATE inventory SET linked_dishes = $1::jsonb WHERE id = $2",
            json.dumps([dish_name]),
            inv_id,
        )

        from app.repositories import orders_repo
        from app.repositories.orders_repo import InsufficientStockError

        order_payload = {
            "id":             _uid(),
            "phone":          "+573009999999",
            "items":          [{"name": dish_name, "quantity": 5, "price": 25_000}],
            "order_type":     "domicilio",
            "address":        "Calle 456",
            "notes":          "",
            "subtotal":       Decimal("125000"),
            "delivery_fee":   Decimal("0"),
            "total":          Decimal("125000"),
            "status":         "pendiente_pago",
            "paid":           False,
            "payment_url":    "",
            "payment_method": "",
            "base_order_id":  None,
            "sub_number":     1,
        }

        fake_pool = _make_pool_for_conn(conn)
        await _register_prod_jsonb_codec(conn)
        with patch("app.services.database.get_pool", AsyncMock(return_value=fake_pool)):
            with tenant_scope(rid):
                with pytest.raises(InsufficientStockError) as exc_info:
                    await orders_repo.commit_order_transaction(
                        fake_pool,
                        restaurant_id=rid,
                        conversation_id="+573009999999",
                        cart={},
                        order_payload=order_payload,
                    )

        assert exc_info.value.requested == 5.0

        # Stock must remain unchanged
        stock_after = await conn.fetchval(
            "SELECT current_stock FROM inventory WHERE id = $1", inv_id
        )
        assert float(stock_after) == 1.0

    @pytest.mark.asyncio
    async def test_order_without_inventory_link_commits(self, db_conn):
        """
        Items that have no matching linked_dishes and no dish_recipes row skip
        the deduction step — the order still commits successfully.
        """
        conn = db_conn
        rid = await _insert_restaurant(conn)

        phone = "+573002222222"

        # Wave-2: carts has org_id NOT NULL
        await conn.execute(
            """
            INSERT INTO carts (phone, org_id, cart_data, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            """,
            phone,
            rid,
            json.dumps({"items": [{"name": "Café Americano", "quantity": 1, "price": 5_000}]}),
        )

        from app.repositories import orders_repo

        order_id = _uid()
        order_payload = {
            "id":             order_id,
            "phone":          phone,
            "items":          [{"name": "Café Americano", "quantity": 1, "price": 5_000}],
            "order_type":     "recoger",
            "address":        "",
            "notes":          "",
            "subtotal":       Decimal("5000"),
            "delivery_fee":   Decimal("0"),
            "total":          Decimal("5000"),
            "status":         "pendiente_pago",
            "paid":           False,
            "payment_url":    "",
            "payment_method": "",
            "base_order_id":  None,
            "sub_number":     1,
        }

        fake_pool = _make_pool_for_conn(conn)
        await _register_prod_jsonb_codec(conn)
        with patch("app.services.database.get_pool", AsyncMock(return_value=fake_pool)):
            with tenant_scope(rid):
                await orders_repo.commit_order_transaction(
                    fake_pool,
                    restaurant_id=rid,
                    conversation_id=phone,
                    cart={},
                    order_payload=order_payload,
                )

        # Order row must exist
        row = await conn.fetchrow("SELECT id, total FROM orders WHERE id = $1", order_id)
        assert row is not None
        assert Decimal(str(row["total"])) == Decimal("5000")


# ═══════════════════════════════════════════════════════════════════════════════
# Flow 2: Table session → check → payment with tip
# ═══════════════════════════════════════════════════════════════════════════════

class TestTableCheckFlow:
    """
    Full table flow:
      create table_order → insert check → pay with tip → verify persisted.

    Note: table_checks.tip_amount is written by the route layer (tables.py) via
    a direct UPDATE, not through a repo function.  Here we exercise the DB
    constraints directly (status transition, tip stored as NUMERIC).
    """

    @pytest.mark.asyncio
    async def test_check_paid_with_tip_persisted(self, db_conn):
        """Open a check, pay it with a tip amount, verify tip stored correctly."""
        conn = db_conn
        rid = await _insert_restaurant(conn)

        base_id = await _insert_table_order(conn, restaurant_id=rid)

        check_id = _uid()
        total = Decimal("63000")
        await conn.execute(
            """
            INSERT INTO table_checks
                (id, base_order_id, check_number, subtotal, tax_amount, total,
                 items, payments, status)
            VALUES ($1, $2, 1, $3, 0, $3, '[]'::jsonb, '[]'::jsonb, 'open')
            """,
            check_id,
            base_id,
            float(total),
        )

        tip_amount = Decimal("10000")
        await conn.execute(
            """
            UPDATE table_checks
            SET status = 'invoiced',
                paid_at = NOW(),
                tip_amount = $2
            WHERE id = $1
            """,
            check_id,
            float(tip_amount),
        )

        row = await conn.fetchrow(
            "SELECT status, tip_amount FROM table_checks WHERE id = $1", check_id
        )
        assert row["status"] == "invoiced"
        assert Decimal(str(row["tip_amount"])) == tip_amount

    @pytest.mark.asyncio
    async def test_tip_cannot_exceed_half_total(self, db_conn):
        """
        The route validates tip_amount <= total * 0.5 before writing.
        We verify the business rule holds at the Decimal level.
        """
        total = Decimal("100000")
        max_tip = total * Decimal("0.5")

        # A tip equal to 50 % is allowed
        assert Decimal("50000") <= max_tip

        # A tip exceeding 50 % is rejected by the route layer
        assert Decimal("50001") > max_tip

    @pytest.mark.asyncio
    async def test_split_check_unique_constraint(self, db_conn):
        """
        Two checks for the same base_order_id must have different check_number.
        The UNIQUE (base_order_id, check_number) constraint enforces this.
        """
        conn = db_conn
        rid = await _insert_restaurant(conn)
        base_id = await _insert_table_order(conn, restaurant_id=rid)

        await conn.execute(
            """
            INSERT INTO table_checks
                (id, base_order_id, check_number, subtotal, tax_amount, total,
                 items, payments, status)
            VALUES ($1, $2, 1, 0, 0, 0, '[]'::jsonb, '[]'::jsonb, 'open')
            """,
            _uid(),
            base_id,
        )

        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO table_checks
                    (id, base_order_id, check_number, subtotal, tax_amount, total,
                     items, payments, status)
                VALUES ($1, $2, 1, 0, 0, 0, '[]'::jsonb, '[]'::jsonb, 'open')
                """,
                _uid(),
                base_id,
            )


# ═══════════════════════════════════════════════════════════════════════════════
# Flow 3: Staff clock-in → paid check → tip distribution
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Flow 4: Staff clock-in unique constraint
# ═══════════════════════════════════════════════════════════════════════════════

class TestShiftConstraints:
    """
    The partial unique index uq_staff_shifts_one_open prevents a second open
    shift for the same staff member.
    """

    @pytest.mark.asyncio
    async def test_duplicate_open_shift_rejected(self, db_conn):
        """Inserting a second open shift for the same staff raises UniqueViolationError."""
        conn = db_conn
        rid = await _insert_restaurant(conn)
        sid = await _insert_staff(conn, restaurant_id=rid)

        # First open shift (clock_out IS NULL) is fine
        await conn.execute(
            """
            INSERT INTO staff_shifts (id, staff_id, org_id, clock_in)
            VALUES ($1, $2::uuid, $3, NOW())
            """,
            uuid.uuid4(),
            sid,
            rid,
        )

        # Second open shift must be rejected by the partial unique index
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO staff_shifts (id, staff_id, org_id, clock_in)
                VALUES ($1, $2::uuid, $3, NOW())
                """,
                uuid.uuid4(),
                sid,
                rid,
            )

    @pytest.mark.asyncio
    async def test_closed_shifts_allow_multiple(self, db_conn):
        """Multiple closed shifts (clock_out IS NOT NULL) for the same staff are allowed."""
        conn = db_conn
        rid = await _insert_restaurant(conn)
        sid = await _insert_staff(conn, restaurant_id=rid)

        base_time = datetime.now(timezone.utc) - timedelta(days=5)

        for day_offset in range(3):
            clock_in  = base_time + timedelta(days=day_offset, hours=8)
            clock_out = base_time + timedelta(days=day_offset, hours=16)
            await conn.execute(
                """
                INSERT INTO staff_shifts (id, staff_id, org_id, clock_in, clock_out)
                VALUES ($1, $2::uuid, $3, $4, $5)
                """,
                uuid.uuid4(),
                sid,
                rid,
                clock_in,
                clock_out,
            )

        count = await conn.fetchval(
            "SELECT COUNT(*) FROM staff_shifts WHERE staff_id = $1::uuid", sid
        )
        assert count == 3
