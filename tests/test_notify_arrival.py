"""
tests/test_notify_arrival.py
============================
Integration tests for the pickup arrival notification flow:

  agent_external.execute_external_action(action="notify_arrival", ...)

These tests run against TEST_DATABASE_URL and verify that:
  1. notify_arrival creates a waiter_alert row with alert_type='customer_arrived'.
  2. notify_arrival is idempotent within a 2-minute window — sending the
     "ya llegué" message multiple times must NOT mint duplicate alerts.

Both tests use the _ConnProxy / _PoolShim pattern to work around
asyncpg.Connection.__slots__ and patch app.services.database.get_pool so the
agent_external code reads from the test DB.

Skipped automatically when TEST_DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── _ConnProxy / _PoolShim (asyncpg __slots__ workaround) ────────────────────


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


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool monkeypatched."""
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
async def org_id(db_conn):
    """Create an org for this test run and return its id."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "NotifyArrivalTestOrg", f"notify-arrival-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org_id_):
    """Set the GUC that RLS reads to enforce tenant isolation."""
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id_),
    )


async def _seed_pickup_order(conn, org_id_, *, phone, bot_number, status="listo"):
    """Insert a pickup order in the given status. Returns the order id."""
    order_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO orders
            (id, org_id, phone, bot_number, order_type, status, paid,
             total, subtotal, items)
        VALUES ($1, $2, $3, $4, 'recoger', $5, true,
                15000, 15000, '[]'::jsonb)
        """,
        order_id, org_id_, phone, bot_number, status,
    )
    return order_id


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_arrival_creates_waiter_alert(db_conn, org_id):
    """notify_arrival must mint a waiter_alert row with alert_type='customer_arrived'."""
    from app.services.agent_external import execute_external_action
    from app.services.tenant_context import tenant_scope

    phone = "+573009990001"
    bot_number = "+570000000001"

    await _set_scope(db_conn, org_id)
    order_id = await _seed_pickup_order(
        db_conn, org_id, phone=phone, bot_number=bot_number, status="listo"
    )

    with tenant_scope(org_id):
        reply = await execute_external_action(
            parsed={"action": "notify_arrival"},
            phone=phone,
            bot_number=bot_number,
            restaurant_obj={"id": org_id},
            routing_context={},
            reply="",
        )

    # Reply must inform the customer the team has been notified
    assert any(kw in reply.lower() for kw in ("avisamos", "equipo", "entregan", "perfecto"))

    # waiter_alerts row was inserted with the right alert_type
    rows = await db_conn.fetch(
        "SELECT id, alert_type, phone, bot_number, message, dismissed "
        "FROM waiter_alerts "
        "WHERE phone = $1 AND bot_number = $2 AND alert_type = 'customer_arrived'",
        phone, bot_number,
    )
    assert len(rows) == 1, (
        f"Expected exactly 1 customer_arrived alert, got {len(rows)}"
    )
    assert rows[0]["dismissed"] is False
    assert order_id in rows[0]["message"]


@pytest.mark.asyncio
async def test_notify_arrival_idempotent_within_2min(db_conn, org_id):
    """Two notify_arrival calls within 2 min must not create duplicate alerts.

    Customers often spam "ya llegué", "estoy aquí", "vine a recoger" in the
    same 30-second window. Each LLM turn fires the tool, so without dedup
    the staff inbox gets buzzed N times per arrival.
    """
    from app.services.agent_external import execute_external_action
    from app.services.tenant_context import tenant_scope

    phone = "+573009990002"
    bot_number = "+570000000002"

    await _set_scope(db_conn, org_id)
    await _seed_pickup_order(
        db_conn, org_id, phone=phone, bot_number=bot_number, status="listo"
    )

    # Fire the tool 3 times in rapid succession — like a real customer would
    with tenant_scope(org_id):
        for _ in range(3):
            await execute_external_action(
                parsed={"action": "notify_arrival"},
                phone=phone,
                bot_number=bot_number,
                restaurant_obj={"id": org_id},
                routing_context={},
                reply="",
            )

    rows = await db_conn.fetch(
        "SELECT id FROM waiter_alerts "
        "WHERE phone = $1 AND bot_number = $2 AND alert_type = 'customer_arrived'",
        phone, bot_number,
    )
    assert len(rows) == 1, (
        f"Expected exactly 1 alert after 3 rapid calls, got {len(rows)}. "
        "Idempotency guard is missing or broken."
    )


@pytest.mark.asyncio
async def test_notify_arrival_no_active_order_returns_message(db_conn, org_id):
    """Customer with no active pickup order gets a polite refusal — no alert is created."""
    from app.services.agent_external import execute_external_action
    from app.services.tenant_context import tenant_scope

    phone = "+573009990003"
    bot_number = "+570000000003"

    # Note: we do NOT seed an order here. The customer has nothing to pick up.
    await _set_scope(db_conn, org_id)

    with tenant_scope(org_id):
        reply = await execute_external_action(
            parsed={"action": "notify_arrival"},
            phone=phone,
            bot_number=bot_number,
            restaurant_obj={"id": org_id},
            routing_context={},
            reply="",
        )

    assert "no tienes" in reply.lower() or "no hay" in reply.lower(), (
        f"Expected refusal message, got: {reply!r}"
    )

    # No waiter_alert was created
    rows = await db_conn.fetch(
        "SELECT id FROM waiter_alerts "
        "WHERE phone = $1 AND bot_number = $2 AND alert_type = 'customer_arrived'",
        phone, bot_number,
    )
    assert len(rows) == 0, (
        f"Expected zero alerts when no pickup order exists, got {len(rows)}"
    )
