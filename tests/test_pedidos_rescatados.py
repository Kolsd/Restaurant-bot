"""
tests/test_pedidos_rescatados.py
=================================
Integration tests for the "Pedidos Rescatados" north-star metric.

CEO definition: any order created via the WhatsApp bot (channel='whatsapp_bot'),
regardless of subsequent status — including cancelled.

Test matrix
-----------
  test_empty_tenant_returns_zeros        — fresh org returns 0, not 500
  test_delivery_bot_orders_counted       — orders with channel='whatsapp_bot' counted
  test_table_bot_orders_counted          — table_orders with channel='whatsapp_bot' counted
  test_manual_orders_excluded            — channel='manual' / 'pos' NOT counted
  test_cancelled_orders_counted          — status='cancelado' IS counted (CEO decision)
  test_period_filter_excludes_old_orders — order 60 days ago excluded from 30d window
  test_global_cross_tenant_aggregation   — org A 5 bot, org B 3 bot, manual orders excluded
  test_tenant_isolation                  — org A cannot see org B's orders
  test_delivery_and_table_split          — count, delivery, table keys correct
"""

import os
import json
import uuid
from datetime import date, datetime, timedelta

import pytest
import asyncpg

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)

# ── Helpers: ConnProxy + PoolShim (same pattern as test_loyalty_aggregates) ──


class _ConnProxy:
    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

    async def execute(self, *a, **kw):
        return await object.__getattribute__(self, "_c").execute(*a, **kw)

    async def fetch(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetch(*a, **kw)

    async def fetchrow(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchrow(*a, **kw)

    async def fetchval(self, *a, **kw):
        return await object.__getattribute__(self, "_c").fetchval(*a, **kw)

    def transaction(self, *a, **kw):
        return object.__getattribute__(self, "_c").transaction(*a, **kw)


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        async def _fake_get_pool():
            return shim

        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_ids(db_conn):
    """Create two isolated orgs and return (org_a_id, org_b_id)."""
    row_a = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "NorthStarTestOrgA", f"ns-test-org-a-{uuid.uuid4().hex[:8]}",
    )
    row_b = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "NorthStarTestOrgB", f"ns-test-org-b-{uuid.uuid4().hex[:8]}",
    )
    return row_a["id"], row_b["id"]


async def _set_org_scope(conn, org_id: int) -> None:
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id),
    )


def _order_id() -> str:
    return f"TEST-{uuid.uuid4().hex[:12].upper()}"


async def _insert_delivery_order(
    conn,
    org_id: int,
    channel: str | None,
    status: str = "entregado",
    created_at: datetime | None = None,
) -> str:
    """Insert a minimal delivery order; returns the order id."""
    oid = _order_id()
    ts = created_at or datetime.utcnow().replace(tzinfo=None)
    await conn.execute(
        """
        INSERT INTO orders
            (id, phone, items, order_type, address, notes,
             subtotal, delivery_fee, total, status, paid,
             org_id, channel, created_at)
        VALUES
            ($1, $2, $3, $4, $5, $6,
             $7, $8, $9, $10, $11,
             $12, $13, $14)
        """,
        oid, "+5730099001",
        json.dumps([{"name": "Arroz con pollo", "qty": 1}]),
        "domicilio", "Calle 10 # 20-30", "",
        20000, 3000, 23000, status, (status == "entregado"),
        org_id, channel, ts,
    )
    return oid


async def _insert_table_order(
    conn,
    org_id: int,
    channel: str | None,
    created_at: datetime | None = None,
) -> str:
    """Insert a minimal table_order; returns the order id."""
    oid = _order_id()
    ts = created_at or datetime.utcnow().replace(tzinfo=None)
    # table_orders requires a table_id that references restaurant_tables — we
    # skip the FK by inserting a restaurant_table row first.
    table_id = f"t-{uuid.uuid4().hex[:8]}"
    await conn.execute(
        """
        INSERT INTO restaurant_tables
            (id, number, name, branch_id, location_id, org_id, active)
        VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        ON CONFLICT (id) DO NOTHING
        """,
        table_id, 99, "Mesa Test", org_id, org_id, org_id,
    )
    await conn.execute(
        """
        INSERT INTO table_orders
            (id, table_id, table_name, phone, items, status, total,
             base_order_id, sub_number, station, org_id, channel, created_at)
        VALUES
            ($1, $2, $3, $4, $5, $6, $7,
             $8, $9, $10, $11, $12, $13)
        """,
        oid, table_id, "Mesa Test", "manual",
        json.dumps([{"name": "Jugo", "qty": 1}]),
        "recibido", 8000,
        oid, 1, "all", org_id, channel, ts,
    )
    return oid


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_tenant_returns_zeros(db_conn, org_ids):
    """Fresh org with no orders returns 0, not a 500."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["count"] == 0
    assert result["delivery"] == 0
    assert result["table"] == 0


@pytest.mark.asyncio
async def test_delivery_bot_orders_counted(db_conn, org_ids):
    """Delivery orders with channel='whatsapp_bot' are counted."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    # Insert 3 bot orders + 1 manual order
    for _ in range(3):
        await _insert_delivery_order(db_conn, org_a, channel="whatsapp_bot")
    await _insert_delivery_order(db_conn, org_a, channel="manual")

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["delivery"] == 3
    assert result["count"] == 3  # manual excluded


@pytest.mark.asyncio
async def test_table_bot_orders_counted(db_conn, org_ids):
    """table_orders with channel='whatsapp_bot' are counted."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    # Insert 2 bot table orders + 1 pos order
    for _ in range(2):
        await _insert_table_order(db_conn, org_a, channel="whatsapp_bot")
    await _insert_table_order(db_conn, org_a, channel="pos")

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["table"] == 2
    assert result["count"] == 2  # pos excluded


@pytest.mark.asyncio
async def test_manual_orders_excluded(db_conn, org_ids):
    """channel='manual', 'pos', NULL — none of these count as rescatados."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    await _insert_delivery_order(db_conn, org_a, channel="manual")
    await _insert_delivery_order(db_conn, org_a, channel="web")
    await _insert_delivery_order(db_conn, org_a, channel=None)
    await _insert_table_order(db_conn, org_a, channel="pos")

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["count"] == 0


@pytest.mark.asyncio
async def test_cancelled_orders_counted(db_conn, org_ids):
    """Cancelled orders ARE counted — CEO confirmed: total demand captured."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    await _insert_delivery_order(
        db_conn, org_a, channel="whatsapp_bot", status="cancelado"
    )

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["count"] == 1


@pytest.mark.asyncio
async def test_period_filter_excludes_old_orders(db_conn, org_ids):
    """An order from 60 days ago is NOT included in a 30d window."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    old_ts = datetime.utcnow().replace(tzinfo=None) - timedelta(days=60)
    await _insert_delivery_order(
        db_conn, org_a, channel="whatsapp_bot", created_at=old_ts
    )
    # Also one recent order
    await _insert_delivery_order(db_conn, org_a, channel="whatsapp_bot")

    today = date.today()
    period_start = today - timedelta(days=29)
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(period_start, today)

    # Only the recent one should be included
    assert result["count"] == 1


@pytest.mark.asyncio
async def test_delivery_and_table_split(db_conn, org_ids):
    """count = delivery + table, split keys reflect source correctly."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, _ = org_ids
    await _set_org_scope(db_conn, org_a)

    # 4 delivery bot orders + 3 table bot orders
    for _ in range(4):
        await _insert_delivery_order(db_conn, org_a, channel="whatsapp_bot")
    for _ in range(3):
        await _insert_table_order(db_conn, org_a, channel="whatsapp_bot")

    today = date.today()
    with tenant_scope(org_a):
        result = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result["delivery"] == 4
    assert result["table"] == 3
    assert result["count"] == 7


@pytest.mark.asyncio
async def test_tenant_isolation(db_conn, org_ids):
    """Org A's orders are NOT visible when scoped to org B."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados
    from app.services.tenant_context import tenant_scope

    org_a, org_b = org_ids

    # Seed 5 bot orders for org A
    await _set_org_scope(db_conn, org_a)
    for _ in range(5):
        await _insert_delivery_order(db_conn, org_a, channel="whatsapp_bot")

    today = date.today()
    # Query scoped to org B — must see 0
    await _set_org_scope(db_conn, org_b)
    with tenant_scope(org_b):
        result_b = await db_count_pedidos_rescatados(today.replace(day=1), today)

    assert result_b["count"] == 0, "Org B should NOT see Org A's orders (tenant isolation)"


@pytest.mark.asyncio
async def test_global_cross_tenant_aggregation(db_conn, org_ids):
    """Global aggregation: org A=5 bot, org B=3 bot, manual orders excluded."""
    from app.repositories.north_star_repo import db_count_pedidos_rescatados_global
    from app.services.tenant_context import bypass_tenant_scope

    org_a, org_b = org_ids

    # Seed org A: 5 bot delivery + 2 manual
    await _set_org_scope(db_conn, org_a)
    for _ in range(5):
        await _insert_delivery_order(db_conn, org_a, channel="whatsapp_bot")
    for _ in range(2):
        await _insert_delivery_order(db_conn, org_a, channel="manual")

    # Seed org B: 3 bot delivery + 1 pos table
    await _set_org_scope(db_conn, org_b)
    for _ in range(3):
        await _insert_delivery_order(db_conn, org_b, channel="whatsapp_bot")
    await _insert_table_order(db_conn, org_b, channel="pos")

    today = date.today()
    period_start = today.replace(day=1)

    with bypass_tenant_scope("test_global_cross_tenant"):
        ranking = await db_count_pedidos_rescatados_global(period_start, today)

    # Find our two test orgs in the ranking
    counts = {r["org_id"]: r["count"] for r in ranking}
    assert counts.get(org_a) == 5, f"Org A should have 5, got {counts.get(org_a)}"
    assert counts.get(org_b) == 3, f"Org B should have 3, got {counts.get(org_b)}"

    # Sorted descending by count: org A (5) must appear before org B (3)
    ids_in_order = [r["org_id"] for r in ranking]
    if org_a in ids_in_order and org_b in ids_in_order:
        assert ids_in_order.index(org_a) < ids_in_order.index(org_b), \
            "Ranking should be sorted descending (org A > org B)"
