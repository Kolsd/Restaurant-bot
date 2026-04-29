"""
tests/test_anti_impostor_flow.py
=================================
Integration tests for Capa 3 — Anti-impostor first-order validation.
See docs/MESA_QR_ARCHITECTURE.md §"Capa 3".

Tests require TEST_DATABASE_URL (real Postgres). Without it all tests are
skipped.

Fixture pattern: same as test_join_code_flow.py / test_loyalty_aggregates.py.

Coverage:
  1. test_first_order_pending_when_unverified
  2. test_first_order_not_pending_when_verified
  3. test_second_order_not_pending_in_unverified_session
  4. test_confirm_real_releases_pending_orders
  5. test_confirm_real_marks_session_verified
  6. test_mark_ghost_cancels_orders
  7. test_mark_ghost_closes_sessions
  8. test_mark_ghost_blocks_phones_24h
  9. test_kds_excludes_pending_orders_from_kitchen
 10. test_blocklist_expiry_via_cleanup
"""

from __future__ import annotations

import os
import secrets

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope, bypass_tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

_needs_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Proxy / shim helpers ──────────────────────────────────────────────────────

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


async def _set_scope(conn, org_id: int) -> None:
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)", str(org_id)
    )


@pytest.fixture
async def org_id(db_conn):
    """Create an isolated org and set RLS scope."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "AntiImpostorTestOrg",
        f"anti-impostor-test-{secrets.token_hex(4)}",
    )
    oid = row["id"]
    await _set_scope(db_conn, oid)
    return oid


@pytest.fixture
async def location_id(db_conn, org_id):
    row = await db_conn.fetchrow(
        """
        INSERT INTO locations
            (org_id, name, code, address, whatsapp_number)
        VALUES ($1, 'TestBranchAI', $2, 'AI St 1', $3)
        RETURNING id
        """,
        org_id,
        f"ai-branch-{secrets.token_hex(4)}",
        f"57900{secrets.token_hex(3).lower()}",
    )
    return row["id"]


# ── Seed helpers ──────────────────────────────────────────────────────────────


async def _make_table(db_conn, org_id: int, loc_id: int, suffix: str = "A") -> str:
    table_id = f"table-ai-test-{suffix}-{secrets.token_hex(3)}"
    await db_conn.execute(
        """
        INSERT INTO restaurant_tables
            (id, number, name, branch_id, location_id, org_id, active)
        VALUES ($1, 99, $2, $3::integer, $4::bigint, $5::bigint, TRUE)
        ON CONFLICT (id) DO NOTHING
        """,
        table_id,
        f"Mesa {suffix}",
        loc_id,
        loc_id,
        org_id,
    )
    return table_id


async def _make_session(
    db_conn,
    phone: str,
    bot: str,
    table_id: str,
    table_name: str,
    org_id: int,
    loc_id: int,
    verified: bool = False,
) -> dict:
    row = await db_conn.fetchrow(
        """
        INSERT INTO table_sessions
            (phone, bot_number, table_id, table_name,
             org_id, location_id, status, verified, last_activity)
        VALUES ($1, $2, $3, $4, $5, $6, 'active', $7, NOW())
        RETURNING id, verified
        """,
        phone,
        bot,
        table_id,
        table_name,
        org_id,
        loc_id,
        verified,
    )
    return dict(row)


async def _make_table_order(
    db_conn,
    phone: str,
    bot: str,
    table_id: str,
    org_id: int,
    loc_id: int,
    pending_table_validation: bool = False,
    status: str = "pendiente",
) -> str:
    order_id = f"order-ai-{secrets.token_hex(4)}"
    # table_orders NOT NULL columns: id, table_id, table_name, phone, items,
    # org_id, station. No `subtotal` or `order_payload` columns. `total` is
    # nullable. Keep INSERT minimal to those required + the test fields.
    await db_conn.execute(
        """
        INSERT INTO table_orders
            (id, phone, bot_number, table_id, table_name, org_id, location_id,
             station, status, items, total, pending_table_validation, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7,
                'cocina', $8, '[]'::jsonb, 15000, $9, NOW() - INTERVAL '2 minutes')
        """,
        order_id,
        phone,
        bot,
        table_id,
        f"Mesa {table_id[-2:]}",   # table_name (NOT NULL)
        org_id,
        loc_id,
        status,
        pending_table_validation,
    )
    return order_id


# ── Tests ─────────────────────────────────────────────────────────────────────


@_needs_db
@pytest.mark.asyncio
async def test_first_order_pending_when_unverified(db_conn, org_id, location_id):
    """db_session_is_verified returns False for a new (unverified) session.
    db_session_has_prior_orders returns False when no orders exist yet.
    These two checks together trigger pending_table_validation=True in the bot.
    """
    from app.repositories.tables_repo import (
        db_session_is_verified,
        db_session_has_prior_orders,
    )

    bot = "57999011001"
    phone = "573001210001"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T1")

    with tenant_scope(org_id):
        # Unverified session (default).
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T1",
                            org_id, location_id, verified=False)

        is_verified = await db_session_is_verified(phone, bot)
        has_prior = await db_session_has_prior_orders(phone, bot)

    assert is_verified is False, (
        "New session must NOT be verified — should trigger pending_table_validation hold"
    )
    assert has_prior is False, (
        "No orders yet — second check also requires hold on first order"
    )
    # Bot logic: _needs_validation = (not is_verified) and (not has_prior) = True
    assert (not is_verified) and (not has_prior), (
        "Both conditions together must trigger the pending_table_validation hold"
    )


@_needs_db
@pytest.mark.asyncio
async def test_first_order_not_pending_when_verified(db_conn, org_id, location_id):
    """When the session is already verified, db_session_is_verified returns True
    and no hold is applied to new orders.
    """
    from app.repositories.tables_repo import db_session_is_verified

    bot = "57999011002"
    phone = "573001210002"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T2")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T2",
                            org_id, location_id, verified=True)

        is_verified = await db_session_is_verified(phone, bot)

    assert is_verified is True, (
        "Verified session must return True — first order goes straight to kitchen"
    )


@_needs_db
@pytest.mark.asyncio
async def test_second_order_not_pending_in_unverified_session(db_conn, org_id, location_id):
    """When session is unverified BUT a prior pending order already exists,
    db_session_has_prior_orders returns True.  The bot skips the hold for
    the second+ order (waiter is already coming).
    """
    from app.repositories.tables_repo import db_session_has_prior_orders

    bot = "57999011003"
    phone = "573001210003"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T3")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T3",
                            org_id, location_id, verified=False)
        # Insert a first (pending validation) order for this phone.
        await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True, status="pendiente",
        )

        has_prior = await db_session_has_prior_orders(phone, bot)

    assert has_prior is True, (
        "With an existing order, db_session_has_prior_orders must return True "
        "so the second order bypasses the hold."
    )


@_needs_db
@pytest.mark.asyncio
async def test_confirm_real_releases_pending_orders(db_conn, org_id, location_id):
    """db_confirm_table_real sets pending_table_validation=false on all held orders."""
    from app.repositories.tables_repo import db_confirm_table_real

    bot = "57999011004"
    phone = "573001210004"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T4")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T4",
                            org_id, location_id, verified=False)
        # Two held orders.
        oid1 = await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True,
        )
        oid2 = await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True,
        )

        released = await db_confirm_table_real(table_id, org_id, "mesero_test")

        # Verify both orders are now released.
        still_pending = await db_conn.fetchval(
            "SELECT COUNT(*) FROM table_orders "
            "WHERE table_id=$1 AND pending_table_validation=true",
            table_id,
        )

    assert released == 2, f"Expected 2 orders released, got {released}"
    assert still_pending == 0, (
        f"Expected 0 still-pending orders after confirm-real, got {still_pending}"
    )


@_needs_db
@pytest.mark.asyncio
async def test_confirm_real_marks_session_verified(db_conn, org_id, location_id):
    """db_confirm_table_real sets table_sessions.verified=true for active sessions."""
    from app.repositories.tables_repo import (
        db_confirm_table_real,
        db_session_is_verified,
    )

    bot = "57999011005"
    phone = "573001210005"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T5")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T5",
                            org_id, location_id, verified=False)

        # Confirm the table is real.
        await db_confirm_table_real(table_id, org_id, "mesero_test")

        # Session should now be verified.
        is_verified = await db_session_is_verified(phone, bot)

    assert is_verified is True, (
        "After db_confirm_table_real, db_session_is_verified must return True "
        "so future orders from this session bypass the hold."
    )


@_needs_db
@pytest.mark.asyncio
async def test_mark_ghost_cancels_orders(db_conn, org_id, location_id):
    """db_mark_table_ghost cancels all pending_table_validation orders on the table."""
    from app.repositories.tables_repo import db_mark_table_ghost

    bot = "57999011006"
    phone = "573001210006"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T6")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T6",
                            org_id, location_id, verified=False)
        oid = await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True, status="pendiente",
        )

        result = await db_mark_table_ghost(table_id, org_id, "mesero_test")

        # Verify order is now cancelled.
        order_status = await db_conn.fetchval(
            "SELECT status FROM table_orders WHERE id=$1",
            oid,
        )

    assert result["orders_cancelled"] == 1, (
        f"Expected 1 order cancelled by mark-ghost, got {result['orders_cancelled']}"
    )
    assert order_status == "cancelado", (
        f"Expected order status='cancelado' after mark-ghost, got {order_status!r}"
    )


@_needs_db
@pytest.mark.asyncio
async def test_mark_ghost_closes_sessions(db_conn, org_id, location_id):
    """db_mark_table_ghost sets active sessions to 'ghost_blocked' status."""
    from app.repositories.tables_repo import db_mark_table_ghost

    bot = "57999011007"
    phone = "573001210007"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T7")

    with tenant_scope(org_id):
        sess = await _make_session(db_conn, phone, bot, table_id, "Mesa C3T7",
                                   org_id, location_id, verified=False)
        await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True,
        )

        result = await db_mark_table_ghost(table_id, org_id, "mesero_test")

        sess_status = await db_conn.fetchval(
            "SELECT status FROM table_sessions WHERE id=$1",
            sess["id"],
        )

    assert sess_status == "ghost_blocked", (
        f"Expected session status='ghost_blocked' after mark-ghost, got {sess_status!r}"
    )
    assert phone in result["phones"], (
        f"Expected phone {phone!r} in mark-ghost result phones, got {result['phones']!r}"
    )


@_needs_db
@pytest.mark.asyncio
async def test_mark_ghost_blocks_phones_24h(db_conn, org_id, location_id):
    """After db_mark_table_ghost, the returned phones can be blocked via
    add_to_blocklist and is_phone_blocked (called in bypass_tenant_scope)
    returns a non-None dict for a blocked phone.
    """
    from app.repositories.tables_repo import db_mark_table_ghost
    from app.repositories.phone_blocklist_repo import add_to_blocklist, is_phone_blocked

    bot = "57999011008"
    phone = "573001210008"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T8")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T8",
                            org_id, location_id, verified=False)
        await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True,
        )

        result = await db_mark_table_ghost(table_id, org_id, "mesero_test")

        # Simulate what the route does: block all phones returned.
        for blocked_phone in result["phones"]:
            await add_to_blocklist(
                phone=blocked_phone,
                org_id=org_id,
                reason="ghost_table_marked",
                blocked_by="mesero_test",
                hours=24,
            )

    # is_phone_blocked is cross-tenant — call in bypass_tenant_scope.
    with bypass_tenant_scope("test: check phone blocked after ghost"):
        block_record = await is_phone_blocked(phone)

    assert block_record is not None, (
        f"Phone {phone!r} should be blocked after mark-ghost, but is_phone_blocked returned None"
    )
    assert block_record["reason"] == "ghost_table_marked", (
        f"Expected reason='ghost_table_marked', got {block_record['reason']!r}"
    )
    assert block_record["org_id"] == org_id


@_needs_db
@pytest.mark.asyncio
async def test_kds_excludes_pending_orders_from_kitchen(db_conn, org_id, location_id):
    """Orders with pending_table_validation=true must NOT appear in a KDS-style
    query that filters them out (mirrors the kitchen.js frontend filter).
    This test verifies the DB data model supports the filter correctly.
    """
    from app.repositories.tables_repo import db_get_table_orders

    bot = "57999011009"
    phone = "573001210009"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="C3T9")

    with tenant_scope(org_id):
        await _make_session(db_conn, phone, bot, table_id, "Mesa C3T9",
                            org_id, location_id, verified=False)

        # Insert one pending-validation order (should be hidden from KDS).
        await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=True, status="pendiente",
        )
        # Insert one regular order (should appear in KDS).
        await _make_table_order(
            db_conn, phone, bot, table_id, org_id, location_id,
            pending_table_validation=False, status="pendiente",
        )

        all_orders = await db_get_table_orders()  # all non-cancelled orders for this org
        kitchen_orders = [o for o in all_orders if not o.get("pending_table_validation")]
        held_orders = [o for o in all_orders if o.get("pending_table_validation")]

    # Only the non-pending order should appear in kitchen view.
    kitchen_for_table = [o for o in kitchen_orders if o.get("table_id") == table_id]
    held_for_table = [o for o in held_orders if o.get("table_id") == table_id]

    assert len(kitchen_for_table) == 1, (
        f"Expected exactly 1 order in KDS view for this table, got {len(kitchen_for_table)}"
    )
    assert len(held_for_table) == 1, (
        f"Expected exactly 1 held order for this table, got {len(held_for_table)}"
    )
    # Verify the kitchen order is not held.
    assert kitchen_for_table[0].get("pending_table_validation") is False, (
        "KDS order must have pending_table_validation=False"
    )


@_needs_db
@pytest.mark.asyncio
async def test_blocklist_expiry_via_cleanup(db_conn, org_id, location_id):
    """cleanup_expired removes rows where blocked_until is in the past.
    We insert a manually-expired row and verify cleanup removes it.
    """
    from app.repositories.phone_blocklist_repo import cleanup_expired

    phone = "573001210010"

    # Insert an already-expired block directly (blocked_until in the past).
    with tenant_scope(org_id):
        await db_conn.execute(
            """
            INSERT INTO phone_blocklist
                (phone, org_id, reason, blocked_by, blocked_until)
            VALUES ($1, $2, 'test_expired_block', 'test_setup',
                    NOW() - INTERVAL '48 hours')
            """,
            phone,
            org_id,
        )

        count_before = await db_conn.fetchval(
            "SELECT COUNT(*) FROM phone_blocklist WHERE phone=$1",
            phone,
        )

    assert count_before == 1, f"Expected 1 expired row before cleanup, got {count_before}"

    # cleanup_expired is cross-tenant — must run in bypass_tenant_scope.
    with bypass_tenant_scope("test: cleanup expired blocklist entries"):
        deleted = await cleanup_expired(older_than_hours=24)

    assert deleted >= 1, (
        f"cleanup_expired must return >= 1 when expired rows exist, got {deleted}"
    )

    # The row should be gone.
    with tenant_scope(org_id):
        count_after = await db_conn.fetchval(
            "SELECT COUNT(*) FROM phone_blocklist WHERE phone=$1 AND reason='test_expired_block'",
            phone,
        )

    assert count_after == 0, (
        f"Expired row must be deleted by cleanup_expired, but count_after={count_after}"
    )
