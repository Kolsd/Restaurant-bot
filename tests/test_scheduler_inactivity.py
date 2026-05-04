"""
tests/test_scheduler_inactivity.py
==================================
Integration tests for the table-session inactivity sweep.

Covers the full lifecycle:
  - db_get_stale_sessions returns sessions stale beyond their threshold
  - db_mark_session_warned flips inactivity_warned and is single-winner
  - db_get_closeable_sessions returns warned sessions stale > 5 min
  - db_close_session drops them out of the active pool

Thresholds (from app/repositories/tables_repo.py db_get_stale_sessions):
  no order:        last_activity < NOW() - 10 min
  order_delivered: last_activity < NOW() - 60 min

After warning, db_get_closeable_sessions picks them up when
inactivity_warned=TRUE AND last_activity < NOW() - 5 min.

The tests bypass the scheduler loop and call the repo functions directly.
The scheduler integration (asyncio.gather of _process_stale_session) is
covered by tests/test_scheduler_leader_renewal.py — here we focus on the
data layer truths the scheduler depends on.

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
async def org_id(db_conn):
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "InactivityTestOrg", f"inactivity-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org_id_):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org_id_),
    )


async def _seed_session(
    conn,
    org_id_,
    *,
    phone,
    bot_number,
    minutes_idle,
    has_order=False,
    order_delivered=False,
    inactivity_warned=False,
    status="active",
):
    """Insert a table_session with last_activity backdated by `minutes_idle`."""
    row = await conn.fetchrow(
        """
        INSERT INTO table_sessions
            (org_id, table_id, table_name, phone, bot_number, status,
             has_order, order_delivered, inactivity_warned, last_activity)
        VALUES ($1, 'T1', 'Mesa 1', $2, $3, $4, $5, $6, $7,
                NOW() - make_interval(mins => $8))
        RETURNING id
        """,
        org_id_, phone, bot_number, status,
        has_order, order_delivered, inactivity_warned, minutes_idle,
    )
    return row["id"]


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_sessions_no_order_picked_after_10min(db_conn, org_id):
    """has_order=FALSE + idle 11 min → returned by db_get_stale_sessions.

    db_get_stale_sessions enters bypass_tenant_scope internally (cross-tenant
    scheduler sweep), so we must NOT be inside tenant_scope when we call it.
    """
    from app.repositories.tables_repo import db_get_stale_sessions

    phone = "+573009990101"
    await _set_scope(db_conn, org_id)
    sid = await _seed_session(
        db_conn, org_id, phone=phone, bot_number="+570000000101",
        minutes_idle=11, has_order=False, order_delivered=False,
    )

    # No tenant_scope wrap — db_get_stale_sessions does its own bypass.
    stale = await db_get_stale_sessions()

    found_ids = [s["id"] for s in stale]
    assert sid in found_ids, (
        f"Expected session {sid} (idle 11min, no order) to be flagged stale. "
        f"Got: {found_ids}"
    )


@pytest.mark.asyncio
async def test_stale_sessions_no_order_idle_under_threshold_skipped(db_conn, org_id):
    """has_order=FALSE + idle only 8 min → NOT flagged stale (threshold is 10)."""
    from app.repositories.tables_repo import db_get_stale_sessions

    await _set_scope(db_conn, org_id)
    sid = await _seed_session(
        db_conn, org_id, phone="+573009990102", bot_number="+570000000102",
        minutes_idle=8, has_order=False,
    )

    stale = await db_get_stale_sessions()

    assert sid not in [s["id"] for s in stale], (
        f"Session {sid} (idle only 8 min) should NOT be flagged stale yet."
    )


@pytest.mark.asyncio
async def test_mark_warned_is_single_winner(db_conn, org_id):
    """db_mark_session_warned must return True only on the first call.

    This is the multi-worker race protection from scheduler.py:113.
    Two workers each call mark_warned on the same session — only one
    gets True back.
    """
    from app.repositories.tables_repo import db_mark_session_warned
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)
    sid = await _seed_session(
        db_conn, org_id, phone="+573009990103", bot_number="+570000000103",
        minutes_idle=11,
    )

    with tenant_scope(org_id):
        first = await db_mark_session_warned(sid)
        second = await db_mark_session_warned(sid)

    assert first is True, "First mark_warned must succeed"
    assert second is False, (
        "Second mark_warned must return False — otherwise two workers "
        "would both send the warning WhatsApp."
    )


@pytest.mark.asyncio
async def test_closeable_after_warning_picked_after_5min(db_conn, org_id):
    """warned=TRUE + idle 6 min → returned by db_get_closeable_sessions.

    db_get_closeable_sessions enters bypass_tenant_scope internally.
    db_close_session is tenant-scoped — we wrap THAT one only.
    """
    from app.repositories.tables_repo import (
        db_get_closeable_sessions, db_close_session,
    )
    from app.services.tenant_context import tenant_scope

    phone = "+573009990104"
    bot_number = "+570000000104"

    await _set_scope(db_conn, org_id)
    sid = await _seed_session(
        db_conn, org_id, phone=phone, bot_number=bot_number,
        minutes_idle=6, has_order=False, inactivity_warned=True,
    )

    closeable = await db_get_closeable_sessions()

    found_ids = [s["id"] for s in closeable]
    assert sid in found_ids, (
        f"Session {sid} (warned + idle 6min) should be closeable. Got: {found_ids}"
    )

    # db_close_session is tenant-scoped — wrap it.
    with tenant_scope(org_id):
        closed = await db_close_session(
            phone=phone, bot_number=bot_number,
            reason="inactivity_timeout", closed_by_username="system",
        )
    assert closed is not None
    assert closed["status"] == "closed"

    closeable_after = await db_get_closeable_sessions()
    assert sid not in [s["id"] for s in closeable_after]
