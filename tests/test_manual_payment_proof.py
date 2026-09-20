"""
tests/test_manual_payment_proof.py
==================================
Integration tests for the manual payment-proof flow (Caja side, Flow #12):

  1. db_attach_order_proof — bot WhatsApp shortcut that links a customer's
     receipt photo to the most recent unpaid delivery/pickup order. Tests
     verify it picks the *latest unpaid* row only and never touches paid /
     terminal-status orders. (No new orders reach this path via WhatsApp
     any more since chunk 9, docs/claude/delivery-web.md, but the function
     stays alive for chat.py's image handler and any pre-existing order.)

The old POST /api/delivery/orders/{id}/validate endpoint this module used to
also cover was deleted in chunk 9 — it exposed org-wide orders and only ever
served the retired WhatsApp delivery/pickup flow. Manual proof validation for
the web channel goes through the sede-scoped /api/staff/delivery/* routes
(app/routes/staff_delivery.py) instead.

Requirements:
  - TEST_DATABASE_URL must be set; otherwise the entire module is skipped
  - Reuses the _ConnProxy / _PoolShim / _set_scope pattern from
    tests/test_loyalty_redemption.py — that fixture is the validated
    reference for integration tests post-Wave-2.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest

# ── Skip entire module if no test DB ─────────────────────────────────────────

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (mirror tests/test_loyalty_redemption.py) ────────────────────────


class _ConnProxy:
    """Thin asyncpg-compatible proxy — works around Connection.__slots__."""

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
    """Yield a rolled-back connection with get_pool mocked (ref. test_loyalty_redemption.py)."""
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
    """Create a single test org and return its id."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "ManualProofTestOrg", f"manual-proof-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org),
    )


async def _seed_order(
    conn,
    org_id_val: int,
    *,
    phone: str,
    bot_number: str,
    status: str = "pendiente",
    paid: bool = False,
    proof_url: str = "",
    created_at_offset_minutes: int = -2,
) -> str:
    """
    Insert a delivery order and return its id.

    Default created_at = NOW() - 2min so the row stays inside any future
    `created_at <= NOW()` window assertion (CLAUDE.md gotcha #6 in
    "No-v2 sprint" section).
    """
    order_id = str(uuid.uuid4())
    created_at = datetime.utcnow() + timedelta(minutes=created_at_offset_minutes)
    await conn.execute(
        """
        INSERT INTO orders
            (id, org_id, phone, bot_number, order_type, status, paid,
             total, subtotal, items, proof_url, created_at)
        VALUES ($1, $2, $3, $4, 'domicilio', $5, $6,
                50000, 50000, '[]'::jsonb, $7, $8)
        """,
        order_id, org_id_val, phone, bot_number, status, paid, proof_url, created_at,
    )
    return order_id


# ── Tests: db_attach_order_proof ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attach_order_proof_links_latest_unpaid(db_conn, org_id):
    """
    Two orders for same phone+bot: one already paid, one pending.
    Attach proof → only the PENDING one gets proof_url set; the paid
    one is left untouched.
    """
    from app.repositories.orders_repo import db_attach_order_proof
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3009000001"
    bot_number = "bot-attach-latest"

    paid_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="confirmado", paid=True, created_at_offset_minutes=-10,
    )
    pending_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="pendiente", paid=False, created_at_offset_minutes=-1,
    )

    media_url = "/api/media/proof_xyz?bot=" + bot_number
    with tenant_scope(org_id):
        attached = await db_attach_order_proof(phone, bot_number, media_url)

    assert attached == pending_id, (
        f"Expected attach to land on the unpaid order {pending_id}, got {attached!r}"
    )

    # Pending one has proof_url; paid one untouched.
    pending_proof = await db_conn.fetchval(
        "SELECT proof_url FROM orders WHERE id=$1", pending_id,
    )
    paid_proof = await db_conn.fetchval(
        "SELECT proof_url FROM orders WHERE id=$1", paid_id,
    )
    assert pending_proof == media_url
    assert paid_proof in ("", None), (
        f"Paid order proof_url must NOT be touched, got: {paid_proof!r}"
    )


@pytest.mark.asyncio
async def test_attach_order_proof_no_match_returns_None(db_conn, org_id):
    """
    No pending delivery/pickup order for phone+bot → returns None,
    no DB writes happen.
    """
    from app.repositories.orders_repo import db_attach_order_proof
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3009000002"
    bot_number = "bot-no-match"

    # Seed only a PAID order — should NOT be picked up.
    paid_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="confirmado", paid=True,
    )

    media_url = "/api/media/orphan_proof?bot=" + bot_number
    with tenant_scope(org_id):
        result = await db_attach_order_proof(phone, bot_number, media_url)

    assert result is None

    # Verify the paid order is untouched.
    proof = await db_conn.fetchval(
        "SELECT proof_url FROM orders WHERE id=$1", paid_id,
    )
    assert proof in ("", None)


@pytest.mark.asyncio
async def test_attach_order_proof_ignores_terminal_status(db_conn, org_id):
    """
    Latest unpaid order is in status='cancelado' — the function must SKIP
    it and look for the next eligible one. If only terminal-status orders
    exist, returns None. We prove both branches:
      (A) terminal-only: returns None
      (B) terminal newer + pending older: pending gets the proof
    """
    from app.repositories.orders_repo import db_attach_order_proof
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3009000003"
    bot_number = "bot-terminal"

    # Branch A: only terminal-status orders.
    cancelled_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="cancelado", paid=False, created_at_offset_minutes=-5,
    )
    delivered_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="entregado", paid=True, created_at_offset_minutes=-3,
    )

    media_url = "/api/media/should_not_attach?bot=" + bot_number
    with tenant_scope(org_id):
        result = await db_attach_order_proof(phone, bot_number, media_url)

    assert result is None, (
        "Cancelado/entregado must not receive proof attachments — got order "
        f"{result!r} attached"
    )

    # Branch B: same setup + add an older PENDING order. Note the function
    # picks the most recent eligible order, where "eligible" excludes
    # cancelado / entregado / paid=true. So the older pending wins.
    pending_id = await _seed_order(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        status="pendiente", paid=False, created_at_offset_minutes=-15,
    )

    media_url_2 = "/api/media/should_attach?bot=" + bot_number
    with tenant_scope(org_id):
        attached = await db_attach_order_proof(phone, bot_number, media_url_2)

    assert attached == pending_id, (
        f"Expected attach to skip terminal orders and land on pending "
        f"{pending_id}, got {attached!r}"
    )

    # Cancelado / entregado ones must still have their original proof_url=''.
    cancelled_proof = await db_conn.fetchval(
        "SELECT proof_url FROM orders WHERE id=$1", cancelled_id,
    )
    delivered_proof = await db_conn.fetchval(
        "SELECT proof_url FROM orders WHERE id=$1", delivered_id,
    )
    assert cancelled_proof in ("", None)
    assert delivered_proof in ("", None)

