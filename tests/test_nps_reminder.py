"""
tests/test_nps_reminder.py
==========================
Integration tests for the NPS 24h re-trigger pipeline (Gap A) and the
caja recent-NPS endpoint (Gap B).

Covers:
  - db_get_nps_waiting_pending_reminder: 24-48h window selection
  - db_mark_nps_reminded: idempotency (second call returns 0)
  - db_cleanup_expired_nps_waiting: only deletes >48h rows
  - db_get_recent_nps_for_caja: phone anonymization + tenant isolation

Requirements:
  - TEST_DATABASE_URL env var must be set (otherwise all tests are skipped)
  - Mirrors the _ConnProxy / _PoolShim fixture pattern from
    test_loyalty_redemption.py.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest


TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers (mirror tests/test_loyalty_redemption.py) ────────────────────────


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


async def _ensure_reminded_at_column(conn) -> None:
    """Add 0063 column inside the test transaction if it isn't present.

    Mirrors the approach in tests/test_delivery_eta.py: run the schema
    additions under the postgres role inside a single transaction so the
    suite passes regardless of whether migration 0063 has been applied
    to TEST_DATABASE_URL. The ALTER is rolled back at teardown so the
    production schema is untouched.
    """
    await conn.execute(
        "ALTER TABLE nps_waiting "
        "ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ DEFAULT NULL"
    )


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """Yield a rolled-back connection with get_pool mocked."""
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
            # ALTER must run before SET LOCAL ROLE (mesio_app cannot DDL).
            await _ensure_reminded_at_column(conn)
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_id(db_conn):
    """Create a single test org and return its id."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "NpsReminderTestOrg", f"nps-rem-{uuid.uuid4().hex[:8]}",
    )
    return row["id"]


async def _set_scope(conn, org):
    await conn.execute(
        "SELECT set_config('app.org_id', $1::text, true)",
        str(org),
    )


async def _seed_nps_waiting(conn, org, phone: str, bot_number: str, hours_ago: int,
                             reminded_hours_ago: int | None = None):
    """Insert a nps_waiting row with created_at offset by hours_ago into the past.

    If reminded_hours_ago is set, also seeds reminded_at to that offset.

    Note: nps_waiting.created_at is TIMESTAMP (naive, see 0001_initial_schema)
    while reminded_at is TIMESTAMPTZ (added by 0063). asyncpg cannot mix
    tz-aware/naive in the same query against a naive column, so we use
    naive UTC for created_at and tz-aware for reminded_at.
    """
    # naive UTC for the TIMESTAMP column
    created_at = datetime.utcnow() - timedelta(hours=hours_ago)
    if reminded_hours_ago is None:
        await conn.execute(
            """INSERT INTO nps_waiting (phone, bot_number, org_id, created_at)
               VALUES ($1, $2, $3, $4)""",
            phone, bot_number, org, created_at,
        )
    else:
        # reminded_at is TIMESTAMPTZ — tz-aware is fine here
        reminded_at = datetime.now(timezone.utc) - timedelta(hours=reminded_hours_ago)
        await conn.execute(
            """INSERT INTO nps_waiting (phone, bot_number, org_id, created_at, reminded_at)
               VALUES ($1, $2, $3, $4, $5)""",
            phone, bot_number, org, created_at, reminded_at,
        )


# ── Gap A: db_get_nps_waiting_pending_reminder ───────────────────────────────


@pytest.mark.asyncio
async def test_get_pending_reminder_returns_only_24_to_48h_window(db_conn, org_id):
    """Insert 3 rows with different ages → only the 25h row qualifies."""
    from app.repositories.conversations_repo import db_get_nps_waiting_pending_reminder
    from app.services.tenant_context import bypass_tenant_scope

    await _set_scope(db_conn, org_id)
    bot_number = "bot-test-window"

    # 5h old → too fresh, no reminder yet
    await _seed_nps_waiting(db_conn, org_id, "3001000001", bot_number, hours_ago=5)
    # 25h old → in the reminder window
    await _seed_nps_waiting(db_conn, org_id, "3001000002", bot_number, hours_ago=25)
    # 50h old → past the window (cleanup zone)
    await _seed_nps_waiting(db_conn, org_id, "3001000003", bot_number, hours_ago=50)

    # cross-tenant scheduler scan — must be in bypass scope
    with bypass_tenant_scope("test_nps_pending_reminder"):
        rows = await db_get_nps_waiting_pending_reminder()

    # Only the 25h row should be returned (filter by phone to scope to our seeds)
    matching = [r for r in rows if r["bot_number"] == bot_number]
    assert len(matching) == 1, f"Expected 1 row in window, got {matching}"
    assert matching[0]["phone"] == "3001000002"
    assert matching[0]["org_id"] == org_id


@pytest.mark.asyncio
async def test_get_pending_reminder_excludes_already_reminded(db_conn, org_id):
    """A 30h row with reminded_at set must NOT appear in the pending list."""
    from app.repositories.conversations_repo import db_get_nps_waiting_pending_reminder
    from app.services.tenant_context import bypass_tenant_scope

    await _set_scope(db_conn, org_id)
    bot_number = "bot-test-excludes"

    # 30h old, already reminded 1h ago
    await _seed_nps_waiting(
        db_conn, org_id, "3001000010", bot_number,
        hours_ago=30, reminded_hours_ago=1,
    )
    # 30h old, NOT reminded
    await _seed_nps_waiting(db_conn, org_id, "3001000011", bot_number, hours_ago=30)

    # cross-tenant scheduler scan — must be in bypass scope
    with bypass_tenant_scope("test_nps_pending_reminder"):
        rows = await db_get_nps_waiting_pending_reminder()
    matching = [r for r in rows if r["bot_number"] == bot_number]
    assert len(matching) == 1
    assert matching[0]["phone"] == "3001000011"


# ── Gap A: db_mark_nps_reminded ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_reminded_sets_timestamp(db_conn, org_id):
    """After mark, reminded_at is NOT NULL."""
    from app.repositories.conversations_repo import db_mark_nps_reminded
    from app.services.tenant_context import bypass_tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000020"
    bot_number = "bot-mark-1"
    await _seed_nps_waiting(db_conn, org_id, phone, bot_number, hours_ago=30)

    # nps_waiting is queried by the scheduler under bypass — match prod usage
    with bypass_tenant_scope("test_mark_reminded"):
        affected = await db_mark_nps_reminded(phone, bot_number)
    assert affected == 1

    row = await db_conn.fetchrow(
        "SELECT reminded_at FROM nps_waiting WHERE phone=$1 AND bot_number=$2",
        phone, bot_number,
    )
    assert row["reminded_at"] is not None


@pytest.mark.asyncio
async def test_mark_reminded_idempotent(db_conn, org_id):
    """Second call to mark on the same row affects 0 rows (WHERE reminded_at IS NULL)."""
    from app.repositories.conversations_repo import db_mark_nps_reminded
    from app.services.tenant_context import bypass_tenant_scope

    await _set_scope(db_conn, org_id)
    phone = "3001000021"
    bot_number = "bot-mark-2"
    await _seed_nps_waiting(db_conn, org_id, phone, bot_number, hours_ago=30)

    with bypass_tenant_scope("test_mark_reminded_idem"):
        first = await db_mark_nps_reminded(phone, bot_number)
        assert first == 1
        second = await db_mark_nps_reminded(phone, bot_number)
        assert second == 0


# ── Gap A: db_cleanup_expired_nps_waiting ────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_deletes_only_after_48h(db_conn, org_id):
    """Insert 30h + 50h rows, cleanup deletes only the 50h row."""
    from app.repositories.conversations_repo import db_cleanup_expired_nps_waiting
    from app.services.tenant_context import bypass_tenant_scope

    await _set_scope(db_conn, org_id)
    bot_number = "bot-cleanup-1"

    await _seed_nps_waiting(db_conn, org_id, "3001000030", bot_number, hours_ago=30)
    await _seed_nps_waiting(db_conn, org_id, "3001000031", bot_number, hours_ago=50)

    # cross-tenant cleanup scheduler — must run under bypass
    with bypass_tenant_scope("test_nps_cleanup"):
        deleted = await db_cleanup_expired_nps_waiting()
    # We track that at least our 50h row was deleted (other tests' residue may
    # also be cleaned up, so >= 1 instead of == 1)
    assert deleted >= 1

    # 30h row should still exist
    survives = await db_conn.fetchval(
        "SELECT COUNT(*) FROM nps_waiting WHERE phone=$1 AND bot_number=$2",
        "3001000030", bot_number,
    )
    assert survives == 1

    # 50h row should be gone
    gone = await db_conn.fetchval(
        "SELECT COUNT(*) FROM nps_waiting WHERE phone=$1 AND bot_number=$2",
        "3001000031", bot_number,
    )
    assert gone == 0


# ── Gap B: db_get_recent_nps_for_caja ────────────────────────────────────────


@pytest.mark.asyncio
async def test_recent_nps_endpoint_anonymizes_phone(db_conn, org_id):
    """Recent NPS rows return phone as ***last4 — never the full number."""
    from app.repositories.restaurant_repo import db_get_recent_nps_for_caja
    from app.services.tenant_context import tenant_scope

    await _set_scope(db_conn, org_id)

    # Seed an NPS response. nps_responses requires bot_number + score.
    await db_conn.execute(
        """INSERT INTO nps_responses
           (phone, bot_number, score, comment, org_id, created_at)
           VALUES ($1, $2, $3, $4, $5, NOW())""",
        "573001234567", "bot-anon-1", 5, "Excelente!", org_id,
    )

    with tenant_scope(org_id):
        items = await db_get_recent_nps_for_caja(limit=10)

    matching = [i for i in items if i["score"] == 5 and "Excelente" in i["comment"]]
    assert matching, f"Expected at least one matching row, got {items}"
    item = matching[0]
    assert item["phone"] == "***4567"
    assert "573001234567" not in str(item)
    assert item["score"] == 5
    assert item["comment"] == "Excelente!"
    assert item["created_at"] is not None


@pytest.mark.asyncio
async def test_recent_nps_endpoint_tenant_isolated(raw_pool, monkeypatch):
    """Org A's NPS responses must NOT leak into Org B's recent feed."""
    from app.repositories.restaurant_repo import db_get_recent_nps_for_caja
    from app.services.tenant_context import tenant_scope
    from app.services import database as db_module

    org_a_id = None
    org_b_id = None

    setup_conn = await raw_pool.acquire()
    try:
        org_a_id = await setup_conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            "NpsIsolA", f"nps-iso-a-{uuid.uuid4().hex[:8]}",
        )
        org_b_id = await setup_conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            "NpsIsolB", f"nps-iso-b-{uuid.uuid4().hex[:8]}",
        )
        # Org A — score 5, distinct comment marker
        await setup_conn.execute(
            """INSERT INTO nps_responses
               (phone, bot_number, score, comment, org_id, created_at)
               VALUES ($1, $2, $3, $4, $5, NOW())""",
            "573009999111", "bot-iso-a", 5, "ISOLATION_MARKER_A", org_a_id,
        )
        # Org B — score 1, distinct comment marker
        await setup_conn.execute(
            """INSERT INTO nps_responses
               (phone, bot_number, score, comment, org_id, created_at)
               VALUES ($1, $2, $3, $4, $5, NOW())""",
            "573009999222", "bot-iso-b", 1, "ISOLATION_MARKER_B", org_b_id,
        )
    finally:
        await raw_pool.release(setup_conn)

    # Build a real-pool shim that drops superuser + scopes per acquire
    class _RealPoolShim:
        def __init__(self, real_pool, scope_org_id):
            self._pool = real_pool
            self._scope_org_id = scope_org_id

        def acquire(self):
            return _RealAcquireCtx(self._pool, self._scope_org_id)

        async def close(self):
            pass

    class _RealAcquireCtx:
        def __init__(self, real_pool, scope_org_id):
            self._pool = real_pool
            self._scope_org_id = scope_org_id
            self._conn = None

        async def __aenter__(self):
            self._conn = await self._pool.acquire()
            await self._conn.execute("SET ROLE mesio_app")
            await self._conn.execute(
                "SELECT set_config('app.org_id', $1::text, false)",
                str(self._scope_org_id),
            )
            return _ConnProxy(self._conn)

        async def __aexit__(self, *_):
            try:
                await self._conn.execute("RESET ROLE")
            except Exception:
                pass
            await self._pool.release(self._conn)
            self._conn = None

    try:
        # ── Scope to Org A → see ONLY Org A's marker ──
        async def _fake_get_pool_a():
            return _RealPoolShim(raw_pool, org_a_id)
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool_a)

        with tenant_scope(org_a_id):
            items_a = await db_get_recent_nps_for_caja(limit=50)

        markers_a = [i["comment"] for i in items_a]
        assert "ISOLATION_MARKER_A" in markers_a, f"Org A should see its own row: {items_a}"
        assert "ISOLATION_MARKER_B" not in markers_a, f"Org A LEAKED Org B data: {items_a}"

        # ── Scope to Org B → see ONLY Org B's marker ──
        async def _fake_get_pool_b():
            return _RealPoolShim(raw_pool, org_b_id)
        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool_b)

        with tenant_scope(org_b_id):
            items_b = await db_get_recent_nps_for_caja(limit=50)

        markers_b = [i["comment"] for i in items_b]
        assert "ISOLATION_MARKER_B" in markers_b, f"Org B should see its own row: {items_b}"
        assert "ISOLATION_MARKER_A" not in markers_b, f"Org B LEAKED Org A data: {items_b}"
    finally:
        # Cleanup both orgs (cascades to nps_responses via FK)
        cleanup_conn = await raw_pool.acquire()
        try:
            for oid in (org_a_id, org_b_id):
                if oid is not None:
                    await cleanup_conn.execute(
                        "DELETE FROM nps_responses WHERE org_id = $1", oid,
                    )
                    await cleanup_conn.execute(
                        "DELETE FROM organizations WHERE id = $1", oid,
                    )
        finally:
            await raw_pool.release(cleanup_conn)
