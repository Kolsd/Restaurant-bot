"""
Integration tests for migration 0039_channel_and_attribution.

Skipped when TEST_DATABASE_URL / DATABASE_URL is not set.

Verifies:
  - New columns exist on orders, table_orders, table_sessions.
  - New indexes exist.
  - NULL insert still works (legacy compat).
  - Non-NULL channel / waiter_staff_id / assigned_staff_id insert works and reads back.
"""
from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.asyncio


def _uid() -> str:
    return str(uuid.uuid4())


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _insert_org_location(conn) -> tuple[int, int]:
    """Insert a minimal org + location. Returns (org_id, location_id)."""
    org_id = await conn.fetchval(
        """INSERT INTO organizations (name, whatsapp_number)
           VALUES ($1, $2)
           RETURNING id""",
        "Test Org " + _uid()[:8],
        "+57" + str(uuid.uuid4().int)[:10],
    )
    loc_id = await conn.fetchval(
        """INSERT INTO locations (org_id, name, code, active, timezone)
           VALUES ($1, 'Main', 'main', true, 'America/Bogota')
           RETURNING id""",
        org_id,
    )
    return org_id, loc_id


async def _column_exists(conn, table: str, column: str) -> bool:
    return await conn.fetchval(
        """SELECT COUNT(*) FROM information_schema.columns
           WHERE table_schema = 'public'
             AND table_name   = $1
             AND column_name  = $2""",
        table, column,
    ) > 0


async def _index_exists(conn, index_name: str) -> bool:
    return await conn.fetchval(
        "SELECT COUNT(*) FROM pg_indexes WHERE indexname = $1",
        index_name,
    ) > 0


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orders_channel_column_exists(db_conn):
    assert await _column_exists(db_conn, "orders", "channel"), \
        "orders.channel column missing — run alembic upgrade head"


@pytest.mark.asyncio
async def test_table_orders_channel_column_exists(db_conn):
    assert await _column_exists(db_conn, "table_orders", "channel"), \
        "table_orders.channel column missing — run alembic upgrade head"


@pytest.mark.asyncio
async def test_table_orders_waiter_staff_id_column_exists(db_conn):
    assert await _column_exists(db_conn, "table_orders", "waiter_staff_id"), \
        "table_orders.waiter_staff_id column missing — run alembic upgrade head"


@pytest.mark.asyncio
async def test_table_sessions_assigned_staff_id_column_exists(db_conn):
    assert await _column_exists(db_conn, "table_sessions", "assigned_staff_id"), \
        "table_sessions.assigned_staff_id column missing — run alembic upgrade head"


@pytest.mark.asyncio
async def test_orders_channel_index_exists(db_conn):
    assert await _index_exists(db_conn, "ix_orders_channel"), \
        "ix_orders_channel index missing — run alembic upgrade head"


@pytest.mark.asyncio
async def test_table_orders_channel_index_exists(db_conn):
    assert await _index_exists(db_conn, "ix_table_orders_channel"), \
        "ix_table_orders_channel index missing"


@pytest.mark.asyncio
async def test_table_orders_waiter_index_exists(db_conn):
    assert await _index_exists(db_conn, "ix_table_orders_waiter"), \
        "ix_table_orders_waiter index missing"


@pytest.mark.asyncio
async def test_table_sessions_assigned_staff_index_exists(db_conn):
    assert await _index_exists(db_conn, "ix_table_sessions_assigned_staff"), \
        "ix_table_sessions_assigned_staff index missing"


@pytest.mark.asyncio
async def test_orders_null_channel_insert(db_conn):
    """Legacy rows with NULL channel must still work."""
    org_id, _ = await _insert_org_location(db_conn)
    order_id = f"TEST-{_uid()[:8]}"
    await db_conn.execute(
        """INSERT INTO orders
               (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, org_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
        order_id, "+5730012345", json.dumps([{"name": "Ajiaco", "qty": 1}]),
        "domicilio", "Calle 123", "",
        15000, 0, 15000, "pendiente", False, org_id,
    )
    row = await db_conn.fetchrow("SELECT channel FROM orders WHERE id = $1", order_id)
    assert row is not None
    assert row["channel"] is None


@pytest.mark.asyncio
async def test_orders_channel_insert_and_readback(db_conn):
    """Non-NULL channel must persist and be readable."""
    org_id, _ = await _insert_org_location(db_conn)
    order_id = f"TEST-{_uid()[:8]}"
    await db_conn.execute(
        """INSERT INTO orders
               (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, org_id, channel)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
        order_id, "+5730012345", json.dumps([{"name": "Bandeja", "qty": 1}]),
        "domicilio", "Calle 456", "",
        28000, 3000, 31000, "pendiente", False, org_id, "whatsapp_bot",
    )
    row = await db_conn.fetchrow("SELECT channel FROM orders WHERE id = $1", order_id)
    assert row is not None
    assert row["channel"] == "whatsapp_bot"


async def _insert_table(conn, org_id: int, loc_id: int) -> str:
    """Insert a minimal restaurant_table and return its id."""
    table_id = f"table-{org_id}-{_uid()[:4]}"
    table_num = int(_uid()[:4], 16) % 900 + 100  # random-ish number 100-999
    await conn.execute(
        """INSERT INTO restaurant_tables
               (id, number, name, branch_id, location_id, org_id, active)
           VALUES ($1, $2, $3, $4::bigint, $5::bigint, $6, TRUE)
           ON CONFLICT (id) DO NOTHING""",
        table_id, table_num, f"Mesa {table_num}", loc_id, loc_id, org_id,
    )
    return table_id


@pytest.mark.asyncio
async def test_table_orders_null_channel_insert(db_conn):
    """table_orders with NULL channel (legacy) must work."""
    org_id, loc_id = await _insert_org_location(db_conn)
    table_id = await _insert_table(db_conn, org_id, loc_id)
    order_id = f"MESA-{_uid()[:6]}"
    await db_conn.execute(
        """INSERT INTO table_orders
               (id, table_id, table_name, phone, items, status, total,
                base_order_id, sub_number, station, org_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
        order_id, table_id, "Mesa 1", "manual",
        json.dumps([{"name": "Limonada", "qty": 2}]),
        "recibido", 8000, order_id, 1, "all", org_id,
    )
    row = await db_conn.fetchrow(
        "SELECT channel, waiter_staff_id FROM table_orders WHERE id = $1", order_id
    )
    assert row is not None
    assert row["channel"] is None
    assert row["waiter_staff_id"] is None


@pytest.mark.asyncio
async def test_table_orders_channel_insert_and_readback(db_conn):
    """table_orders with channel='pos' must persist."""
    org_id, loc_id = await _insert_org_location(db_conn)
    table_id = await _insert_table(db_conn, org_id, loc_id)
    order_id = f"MESA-{_uid()[:6]}"
    await db_conn.execute(
        """INSERT INTO table_orders
               (id, table_id, table_name, phone, items, status, total,
                base_order_id, sub_number, station, org_id, channel)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
        order_id, table_id, "Mesa 2", "caja",
        json.dumps([{"name": "Cerveza", "qty": 3}]),
        "recibido", 21000, order_id, 1, "all", org_id, "pos",
    )
    row = await db_conn.fetchrow(
        "SELECT channel, waiter_staff_id FROM table_orders WHERE id = $1", order_id
    )
    assert row is not None
    assert row["channel"] == "pos"
    assert row["waiter_staff_id"] is None


@pytest.mark.asyncio
async def test_table_sessions_null_assigned_staff_insert(db_conn):
    """table_sessions with NULL assigned_staff_id must work (legacy compat)."""
    org_id, loc_id = await _insert_org_location(db_conn)
    row = await db_conn.fetchrow(
        """INSERT INTO table_sessions
               (phone, bot_number, table_id, table_name, org_id, location_id, status, last_activity)
           VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW())
           RETURNING assigned_staff_id""",
        "+5730099999", "+5730000000",
        f"table-{org_id}-1", f"{org_id}-1",
        org_id, loc_id,
    )
    assert row is not None
    assert row["assigned_staff_id"] is None
