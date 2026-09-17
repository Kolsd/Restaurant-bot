"""
tests/test_realtime.py

Tests for the SSE real-time layer (app/services/realtime.py + the
/api/staff/stream and /api/diner/stream routes).

Sections:
  1. Hub (in-process, no DB, no Redis — matches the local dev/test env per
     CLAUDE.md "no Redis"): publish -> same-org subscriber receives it,
     another org does not, queue overflow -> resync.
  2. Diner topic_filter: another table's event and a non-allowlisted topic
     are dropped; `resync` always bypasses the filter (tested at the
     event_stream level, per the shared contract).
  3. Auth: GET /api/staff/stream and GET /api/diner/stream both 401 without
     credentials. Uses the TestClient's stream() so the (empty) response
     never blocks on an endless generator.
  4. At least 3 repo hooks against TEST_DATABASE_URL: the REAL repo call
     (not a mock) publishes the right topic/org_id — verified by
     subscribing to the hub, per docs/claude/testing.md's integration
     fixture pattern.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.services import realtime
from app.routes.diner import _make_diner_filter

# pyproject.toml sets asyncio_mode = "auto" — `async def test_*` functions run
# without an explicit @pytest.mark.asyncio; sync tests (TestClient, pure
# filter-function tests) are unaffected either way.

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")


# ── 1. Hub (in-process, no DB) ────────────────────────────────────────────────

async def test_publish_delivers_to_same_org_subscriber():
    org_id = 910001
    async with realtime.subscribe(org_id) as queue:
        await realtime.publish(org_id, "waiter_alert.created", location_id=5, table_id="T1", entity_id="42")
        event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event["topic"] == "waiter_alert.created"
    assert event["org_id"] == org_id
    assert event["location_id"] == 5
    assert event["table_id"] == "T1"
    assert event["entity_id"] == "42"
    assert "ts" in event


async def test_publish_does_not_leak_to_other_org():
    org_a, org_b = 910002, 910003
    async with realtime.subscribe(org_a) as queue_a, realtime.subscribe(org_b) as queue_b:
        await realtime.publish(org_a, "table_order.created", table_id="T1")
        event = await asyncio.wait_for(queue_a.get(), timeout=1)
        assert event["org_id"] == org_a
        # org_b's queue must still be empty — nothing crossed the tenant boundary.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue_b.get(), timeout=0.2)


async def test_publish_invalid_topic_is_dropped_not_raised():
    org_id = 910004
    async with realtime.subscribe(org_id) as queue:
        await realtime.publish(org_id, "not_a_real_topic")  # must never raise
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.2)


async def test_queue_overflow_replaces_backlog_with_resync():
    org_id = 910005
    async with realtime.subscribe(org_id) as queue:
        # Fill past maxsize=100 so the hub must drop-and-replace with resync.
        # Exactly one past capacity: the (maxsize+1)-th publish is the one
        # that must observe QueueFull and swap the backlog for a resync
        # marker. Anything published AFTER that lands normally in the
        # now-empty-but-for-the-marker queue — so we stop right at the
        # trigger to assert the swap in isolation.
        for _ in range(realtime._QUEUE_MAXSIZE + 1):
            await realtime.publish(org_id, "table_order.updated", table_id="T1")

        # The ONLY thing left in the queue must be a single resync marker —
        # not a pile of the individual table_order.updated events.
        seen = []
        while True:
            try:
                seen.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        assert len(seen) == 1, f"expected exactly 1 resync marker, got {len(seen)}: {seen}"
        assert seen[0]["topic"] == "resync"
        assert seen[0]["org_id"] == org_id


# ── 2. Diner topic_filter ─────────────────────────────────────────────────────

def test_diner_filter_allows_own_table_allowlisted_topic():
    filt = _make_diner_filter("MESA-7")
    assert filt({"topic": "table_order.updated", "table_id": "MESA-7"}) is True
    assert filt({"topic": "check.updated", "table_id": "MESA-7"}) is True


def test_diner_filter_blocks_other_table():
    filt = _make_diner_filter("MESA-7")
    assert filt({"topic": "table_order.updated", "table_id": "MESA-99"}) is False


def test_diner_filter_blocks_non_allowlisted_topic():
    filt = _make_diner_filter("MESA-7")
    # waiter_alert.* is staff-only per the contract's diner allowlist.
    assert filt({"topic": "waiter_alert.created", "table_id": "MESA-7"}) is False


async def test_event_stream_resync_bypasses_any_filter():
    """resync must reach the client even though the diner filter would
    otherwise block every event on this (mismatched) table_id."""
    org_id = 910006
    filt = _make_diner_filter("MESA-DOES-NOT-MATCH")

    async def _never_disconnect():
        return False

    gen = realtime.event_stream(_never_disconnect, org_id, topic_filter=filt, heartbeat_seconds=100)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert first == "event: ready\ndata: {}\n\n"

    # Overflow this org's queue so the hub emits a resync marker.
    for _ in range(realtime._QUEUE_MAXSIZE + 5):
        await realtime.publish(org_id, "table_order.updated", table_id="MESA-DOES-NOT-MATCH")

    frame = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert frame.startswith("event: resync\n")
    await gen.aclose()


# ── 3. Auth (401 without credentials) ─────────────────────────────────────────

def test_staff_stream_401_without_auth(client):
    resp = client.get("/api/staff/stream")
    assert resp.status_code == 401


def test_diner_stream_401_without_auth(client):
    resp = client.get("/api/diner/stream")
    assert resp.status_code == 401


@pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — hits diner_sessions_repo for real")
def test_diner_stream_401_with_unknown_token(client):
    resp = client.get("/api/diner/stream", headers={"Authorization": "Bearer nonexistent-token"})
    assert resp.status_code == 401


# ── 4. Repo hooks against TEST_DATABASE_URL ───────────────────────────────────

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

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
async def raw_pool():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set — integration tests skipped")
    import asyncpg
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """See docs/claude/testing.md — the validated tenant-scoped integration
    fixture pattern (rolled-back transaction, SET LOCAL ROLE mesio_app so
    FORCE RLS actually enforces)."""
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


async def _seed_org(conn, name: str) -> tuple[int, int]:
    org_id = await conn.fetchval(
        """
        INSERT INTO organizations (name, slug, subscription_plan, subscription_status)
        VALUES ($1, $2, 'pro', 'active')
        RETURNING id
        """,
        name,
        name.lower().replace(" ", "_") + "_" + uuid.uuid4().hex[:8],
    )
    location_id = await conn.fetchval(
        """
        INSERT INTO locations (org_id, name, address, whatsapp_number)
        VALUES ($1, $2, 'Calle 1 # 1-1', $3)
        RETURNING id
        """,
        org_id,
        name + " - Principal",
        "+5730000" + str(org_id).zfill(4),
    )
    return org_id, location_id


@pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")
class TestRepoHooksPublishToHub:
    async def test_db_create_waiter_alert_publishes_waiter_alert_created(self, db_conn):
        from app.repositories import tables_repo
        from app.services.tenant_context import tenant_scope

        org_id, location_id = await _seed_org(db_conn, "RT Waiter Co")

        async with realtime.subscribe(org_id) as queue:
            with tenant_scope(org_id):
                await tables_repo.db_create_waiter_alert(
                    phone="web:test-token", bot_number="bot:rt-test",
                    alert_type="bill", message="La cuenta por favor",
                    table_id="MESA-RT1", table_name="Mesa RT1",
                    location_id=location_id,
                )
            event = await asyncio.wait_for(queue.get(), timeout=2)

        assert event["topic"] == "waiter_alert.created"
        assert event["org_id"] == org_id
        assert event["location_id"] == location_id
        assert event["table_id"] == "MESA-RT1"

    async def test_db_update_table_order_status_publishes_table_order_updated(self, db_conn):
        from app.repositories import tables_repo
        from app.services.tenant_context import tenant_scope

        org_id, location_id = await _seed_org(db_conn, "RT Table Co")
        order_id = f"MESA-RT-{uuid.uuid4().hex[:6]}"

        with tenant_scope(org_id):
            # Seed the base row directly (bypass the hub) so we only observe
            # the status-update publish, not the create one.
            await tables_repo.db_save_table_order({
                "id": order_id, "table_id": "MESA-RT2", "table_name": "Mesa RT2",
                "phone": "web:test-token", "items": [{"name": "Arepa", "quantity": 1}],
                "total": 8000, "org_id": org_id, "branch_id": location_id,
            })

            async with realtime.subscribe(org_id) as queue:
                await tables_repo.db_update_table_order_status(order_id, "en_preparacion")
                event = await asyncio.wait_for(queue.get(), timeout=2)

        assert event["topic"] == "table_order.updated"
        assert event["org_id"] == org_id
        assert event["location_id"] == location_id
        assert event["entity_id"] == order_id

    async def test_db_update_order_status_publishes_order_updated(self, db_conn):
        from app.repositories import orders_repo
        from app.services.tenant_context import tenant_scope

        org_id, location_id = await _seed_org(db_conn, "RT Delivery Co")
        order_id = f"ORD-RT-{uuid.uuid4().hex[:6]}"

        with tenant_scope(org_id):
            # Raw seed INSERT on the test proxy connection — tenant_scope()
            # only pins the ContextVar; it doesn't touch the DB session, so
            # the RLS WITH CHECK GUC must be set explicitly here (unlike the
            # repo call below, which goes through tenant_connection() and
            # sets it itself).
            await db_conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
            await db_conn.execute(
                """
                INSERT INTO orders (id, phone, items, order_type, subtotal, total, org_id, location_id)
                VALUES ($1, 'web:test-token', '[]'::jsonb, 'domicilio', 10000, 10000, $2, $3)
                """,
                order_id, org_id, location_id,
            )

            async with realtime.subscribe(org_id) as queue:
                result = await orders_repo.db_update_order_status(order_id, "confirmado")
                event = await asyncio.wait_for(queue.get(), timeout=2)

        assert result is not None
        assert event["topic"] == "order.updated"
        assert event["org_id"] == org_id
        assert event["entity_id"] == order_id

    async def test_db_cancel_pending_order_publishes_order_updated(self, db_conn):
        from app.repositories import orders_repo
        from app.services.tenant_context import tenant_scope

        org_id, location_id = await _seed_org(db_conn, "RT Cancel Co")
        order_id = f"ORD-RT-{uuid.uuid4().hex[:6]}"
        phone, bot_number = "web:test-token", "bot:rt-cancel-test"

        with tenant_scope(org_id):
            await db_conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
            await db_conn.execute(
                """
                INSERT INTO orders (id, phone, items, order_type, subtotal, total,
                                     org_id, location_id, bot_number, status)
                VALUES ($1, $2, '[]'::jsonb, 'domicilio', 10000, 10000, $3, $4, $5, 'pendiente')
                """,
                order_id, phone, org_id, location_id, bot_number,
            )

            async with realtime.subscribe(org_id) as queue:
                result = await orders_repo.db_cancel_pending_order(phone, bot_number, reason="cliente se arrepintió")
                event = await asyncio.wait_for(queue.get(), timeout=2)

        assert result == {"cancelled": True, "order_id": order_id}
        assert event["topic"] == "order.updated"
        assert event["org_id"] == org_id
        assert event["location_id"] == location_id
        assert event["entity_id"] == order_id

    async def test_db_insert_check_publishes_check_updated(self, db_conn):
        from app.repositories import tables_repo
        from app.services.tenant_context import tenant_scope

        org_id, location_id = await _seed_org(db_conn, "RT Check Co")
        base_order_id = f"MESA-RT-{uuid.uuid4().hex[:6]}"

        with tenant_scope(org_id):
            # Seed the base table_order row so _table_order_group_context can
            # resolve org_id/location_id/table_id for the check.updated event
            # (table_checks itself carries none of those).
            await tables_repo.db_save_table_order({
                "id": base_order_id, "table_id": "MESA-RT3", "table_name": "Mesa RT3",
                "phone": "web:test-token", "items": [{"name": "Jugo", "quantity": 2}],
                "total": 12000, "org_id": org_id, "branch_id": location_id,
            })

            async with realtime.subscribe(org_id) as queue:
                created = await tables_repo.db_insert_check(
                    base_order_id, 1, [{"name": "Jugo", "qty": 2, "subtotal": 12000}],
                    subtotal=12000, tax_amount=0, total=12000,
                )
                event = await asyncio.wait_for(queue.get(), timeout=2)

        assert created is not None
        assert event["topic"] == "check.updated"
        assert event["org_id"] == org_id
        assert event["location_id"] == location_id
        assert event["table_id"] == "MESA-RT3"
        assert event["entity_id"] == f"{base_order_id}-CHK-1"
