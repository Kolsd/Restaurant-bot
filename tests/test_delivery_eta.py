"""
tests/test_delivery_eta.py
==========================
Integration tests for delivery ETA communication.

Migration 0064 adds two columns to `orders`:
  estimated_minutes  INTEGER  NULL
  eta_communicated   BOOLEAN  FALSE

This test module:
  1. Adds the columns inside the test transaction (NULLABLE / DEFAULT FALSE)
     when they don't already exist, so the suite passes both BEFORE and
     AFTER 0064 has been applied to TEST_DATABASE_URL. The transaction is
     rolled back at the end of each test, so the production schema is
     untouched.
  2. Tests db_set_order_eta, db_get_orders_needing_eta_communication,
     db_mark_eta_communicated.
  3. Hits POST /api/delivery/orders/{order_id}/eta to verify range / auth.

Skipped automatically when TEST_DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

# ── Skip module if no test DB ────────────────────────────────────────────────

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


async def _ensure_eta_columns(conn) -> None:
    """Add 0064 columns inside the test transaction if they aren't present.

    Running alembic against the shared TEST_DATABASE_URL needs explicit
    authorization; this helper makes the test work either way. Both ALTERs
    are rolled back at teardown, so production schema is unaffected.
    """
    await conn.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS estimated_minutes INTEGER DEFAULT NULL"
    )
    await conn.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS eta_communicated BOOLEAN NOT NULL DEFAULT FALSE"
    )


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
            # ALTER must run before SET LOCAL ROLE (postgres role can DDL,
            # mesio_app cannot).
            await _ensure_eta_columns(conn)
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_id(db_conn):
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "EtaTestOrg", f"eta-test-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org_id_):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id_),
    )


async def _seed_order(conn, org_id_, **kw):
    """Insert a delivery order and return its id."""
    order_id = str(uuid.uuid4())
    cols = {
        "id": order_id,
        "org_id": org_id_,
        "phone": kw.get("phone", "+573001112233"),
        "bot_number": kw.get("bot_number", "+570000001"),
        "order_type": kw.get("order_type", "domicilio"),
        "status": kw.get("status", "confirmado"),
        "paid": kw.get("paid", True),
        "total": kw.get("total", 35000),
        "subtotal": kw.get("subtotal", 35000),
    }
    await conn.execute(
        """
        INSERT INTO orders
            (id, org_id, phone, bot_number, order_type, status, paid,
             total, subtotal, items)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '[]'::jsonb)
        """,
        cols["id"], cols["org_id"], cols["phone"], cols["bot_number"],
        cols["order_type"], cols["status"], cols["paid"],
        cols["total"], cols["subtotal"],
    )
    return order_id


# ── Repo tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_eta_resets_communicated_flag(db_conn, org_id):
    """db_set_order_eta updates estimated_minutes and resets eta_communicated."""
    from app.repositories.orders_repo import db_set_order_eta
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    order_id = await _seed_order(db_conn, org_id)

    # Pretend a previous ETA was already communicated, so we can verify the
    # reset behaviour (not just the initial set).
    await db_conn.execute(
        "UPDATE orders SET estimated_minutes = 25, eta_communicated = TRUE WHERE id = $1",
        order_id,
    )

    with tenant_scope(org_id):
        row = await db_set_order_eta(order_id, 40)

    assert row is not None
    assert row["estimated_minutes"] == 40
    assert row["eta_communicated"] is False, (
        "Updating ETA must reset eta_communicated so the customer hears "
        "about the new ETA — otherwise stale ETAs go unnoticed."
    )

    # Verify in the table
    db_row = await db_conn.fetchrow(
        "SELECT estimated_minutes, eta_communicated FROM orders WHERE id = $1",
        order_id,
    )
    assert db_row["estimated_minutes"] == 40
    assert db_row["eta_communicated"] is False


@pytest.mark.asyncio
async def test_set_eta_rejects_out_of_range(db_conn, org_id):
    """estimated_minutes must be 1..180 — repo raises ValueError otherwise."""
    from app.repositories.orders_repo import db_set_order_eta
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    order_id = await _seed_order(db_conn, org_id)

    with tenant_scope(org_id):
        with pytest.raises(ValueError):
            await db_set_order_eta(order_id, 0)
        with pytest.raises(ValueError):
            await db_set_order_eta(order_id, 181)
        with pytest.raises(ValueError):
            await db_set_order_eta(order_id, -5)


@pytest.mark.asyncio
async def test_get_orders_needing_eta_returns_only_unsent(db_conn, org_id):
    """Only paid + active + un-communicated orders are returned."""
    from app.repositories.orders_repo import db_get_orders_needing_eta_communication
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    # Eligible: paid + confirmado + ETA set + flag false
    eligible = await _seed_order(db_conn, org_id, paid=True, status="confirmado")
    await db_conn.execute(
        "UPDATE orders SET estimated_minutes = 30, eta_communicated = FALSE WHERE id = $1",
        eligible,
    )

    # Already communicated → skipped
    communicated = await _seed_order(db_conn, org_id, paid=True, status="confirmado")
    await db_conn.execute(
        "UPDATE orders SET estimated_minutes = 25, eta_communicated = TRUE WHERE id = $1",
        communicated,
    )

    # No ETA set → skipped
    no_eta = await _seed_order(db_conn, org_id, paid=True, status="confirmado")

    # Wrong status (entregado) → skipped
    delivered = await _seed_order(db_conn, org_id, paid=True, status="entregado")
    await db_conn.execute(
        "UPDATE orders SET estimated_minutes = 30, eta_communicated = FALSE WHERE id = $1",
        delivered,
    )

    # Unpaid → skipped (paid=FALSE means we haven't validated the proof yet)
    unpaid = await _seed_order(db_conn, org_id, paid=False, status="pendiente")
    await db_conn.execute(
        "UPDATE orders SET estimated_minutes = 30, eta_communicated = FALSE WHERE id = $1",
        unpaid,
    )

    with tenant_scope(org_id):
        pending = await db_get_orders_needing_eta_communication()

    pending_ids = {p["id"] for p in pending}
    assert eligible in pending_ids
    assert communicated not in pending_ids
    assert no_eta not in pending_ids
    assert delivered not in pending_ids
    assert unpaid not in pending_ids


@pytest.mark.asyncio
async def test_mark_eta_communicated_is_single_winner(db_conn, org_id):
    """Two scheduler workers calling mark — only one returns True."""
    from app.repositories.orders_repo import (
        db_set_order_eta, db_mark_eta_communicated,
    )
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    order_id = await _seed_order(db_conn, org_id, paid=True, status="confirmado")

    with tenant_scope(org_id):
        await db_set_order_eta(order_id, 30)
        first = await db_mark_eta_communicated(order_id)
        second = await db_mark_eta_communicated(order_id)

    assert first is True, "First mark must succeed"
    assert second is False, (
        "Second mark must return False — otherwise two scheduler workers "
        "could both WhatsApp the customer the same ETA."
    )


# Endpoint tests (range validation, auth gate) live in
# tests/test_delivery_eta_endpoint.py — kept separate so they don't share the
# raw asyncpg pool fixture with the repo tests above (running TestClient on the
# same event loop as a held asyncpg pool causes pool / connection conflicts).
