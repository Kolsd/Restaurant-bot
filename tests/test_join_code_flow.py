"""
tests/test_join_code_flow.py
============================
Integration tests for Capa 2 — Multi-participant join-code flow.
See docs/MESA_QR_ARCHITECTURE.md §"Capa 2".

Tests require TEST_DATABASE_URL (real Postgres). Without it all tests are
skipped.

Fixture pattern from test_loyalty_aggregates.py:
  - function-scoped pool
  - _ConnProxy / _PoolShim to work around asyncpg Connection.__slots__
  - manual tx.start() / tx.rollback() for teardown
  - SET LOCAL ROLE mesio_app so RLS enforces correctly

Coverage:
  1. test_join_code_generated_for_first_session
  2. test_second_phone_blocked_until_code
  3. test_correct_code_opens_participant_session
  4. test_wrong_code_increments_attempts
  5. test_3_wrong_codes_blocks_phone
  6. test_same_phone_rescan_ok_no_code
  7. test_phone_a_scans_different_table_closes_old
  8. test_join_code_persists_across_session_lifetime
  9. test_join_code_cleared_on_session_close (new session gets new code)
"""

from __future__ import annotations

import os
import re
import secrets

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

# DB-required tests are skipped when TEST_DATABASE_URL is not set.
# Pure unit tests (test_wrong_code_increments_attempts, test_3_wrong_codes_blocks_phone)
# do NOT use TEST_DATABASE_URL and run in every environment.
_needs_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Proxy / shim helpers (same pattern as test_loyalty_aggregates.py) ─────────


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
    """Create an isolated org for the test and set the RLS scope."""
    row = await db_conn.fetchrow(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        "JoinCodeTestOrg",
        f"join-code-test-org-{secrets.token_hex(4)}",
    )
    oid = row["id"]
    await _set_scope(db_conn, oid)
    return oid


@pytest.fixture
async def location_id(db_conn, org_id):
    """Create a location for the org. Note: locations has `code` not `slug`
    (slug lives on organizations); whatsapp_number on the location is unique
    across all locations so we generate a per-test value to avoid collisions
    when tests run in parallel or share a long-lived test DB.
    """
    row = await db_conn.fetchrow(
        """
        INSERT INTO locations
            (org_id, name, code, address, whatsapp_number)
        VALUES ($1, 'TestBranch', $2, 'Test St 1', $3)
        RETURNING id
        """,
        org_id,
        f"test-branch-{secrets.token_hex(4)}",
        f"57999{secrets.token_hex(3).lower()}",
    )
    return row["id"]


async def _make_table(db_conn, org_id: int, loc_id: int, suffix: str = "A") -> str:
    """Insert a restaurant_table row and return its id."""
    table_id = f"table-jc-test-{suffix}-{secrets.token_hex(3)}"
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
    join_code: str | None = None,
) -> dict:
    """Insert a table_sessions row directly and return its id."""
    row = await db_conn.fetchrow(
        """
        INSERT INTO table_sessions
            (phone, bot_number, table_id, table_name,
             org_id, location_id, join_code, status, last_activity)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', NOW())
        RETURNING id, join_code
        """,
        phone,
        bot,
        table_id,
        table_name,
        org_id,
        loc_id,
        join_code,
    )
    return dict(row)


# ── Tests ─────────────────────────────────────────────────────────────────────


@_needs_db
@pytest.mark.asyncio
async def test_join_code_generated_for_first_session(db_conn, org_id, location_id):
    """When the first participant opens a session, db_set_session_join_code
    persists a 4-digit code and db_get_session_join_code returns it."""
    from app.repositories.tables_repo import (
        db_create_table_session,
        db_get_session_join_code,
        db_set_session_join_code,
    )
    from app.services.agent import _generate_join_code

    bot = "57999000001"
    phone = "573001110001"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T1")

    with tenant_scope(org_id):
        session = await db_create_table_session(
            phone, bot, table_id, "Mesa T1",
            org_id=org_id, location_id=location_id,
        )
        code = _generate_join_code()
        # Code must be 4 numeric digits.
        assert re.fullmatch(r"\d{4}", code), f"Generated code is not 4 digits: {code!r}"

        updated = await db_set_session_join_code(session["id"], code)
        assert updated is True, "db_set_session_join_code should return True on first set"

        stored_code = await db_get_session_join_code(table_id, bot)
        assert stored_code == code, (
            f"Expected stored join_code={code!r}, got {stored_code!r}"
        )


@_needs_db
@pytest.mark.asyncio
async def test_second_phone_blocked_until_code(db_conn, org_id, location_id):
    """When phone A has an active session, db_get_active_session_on_table_by_other_phone
    returns that session for phone B. The bot flow should set requires_join_code state."""
    from app.repositories.tables_repo import db_get_active_session_on_table_by_other_phone

    bot = "57999000002"
    phone_a = "573001110002"
    phone_b = "573001110003"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T2")

    with tenant_scope(org_id):
        # Phone A opens the session.
        await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T2",
            org_id, location_id, join_code="1234",
        )

        # Phone B scans the same table — should see A's session.
        other = await db_get_active_session_on_table_by_other_phone(table_id, phone_b)
        assert other is not None, (
            "db_get_active_session_on_table_by_other_phone must return phone A's session "
            "when phone B tries to scan the same table."
        )
        assert other["phone"] == phone_a, (
            f"Expected holder phone={phone_a!r}, got {other['phone']!r}"
        )


@_needs_db
@pytest.mark.asyncio
async def test_correct_code_opens_participant_session(db_conn, org_id, location_id):
    """Phone B provides the correct join_code → db_link_participant_session
    creates a new session for phone B sharing the same table_id."""
    from app.repositories.tables_repo import db_link_participant_session

    bot = "57999000003"
    phone_a = "573001110004"
    phone_b = "573001110005"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T3")
    join_code = "4729"

    with tenant_scope(org_id):
        # Phone A is the host.
        await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T3",
            org_id, location_id, join_code=join_code,
        )

        # Phone B joins with correct code.
        new_session = await db_link_participant_session(
            phone=phone_b,
            bot_number=bot,
            table_id=table_id,
            table_name="Mesa T3",
            join_code=join_code,
            org_id=org_id,
            location_id=location_id,
        )

    assert new_session is not None, (
        "db_link_participant_session must return the new session dict when code is correct."
    )
    assert new_session["phone"] == phone_b
    assert new_session["table_id"] == table_id
    assert new_session["join_code"] == join_code
    assert new_session["status"] == "active"


@_needs_db
@pytest.mark.asyncio
async def test_wrong_code_returns_none(db_conn, org_id, location_id):
    """Phone B provides the WRONG code → db_link_participant_session returns None."""
    from app.repositories.tables_repo import db_link_participant_session

    bot = "57999000004"
    phone_a = "573001110006"
    phone_b = "573001110007"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T4")

    with tenant_scope(org_id):
        await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T4",
            org_id, location_id, join_code="9999",
        )

        result = await db_link_participant_session(
            phone=phone_b,
            bot_number=bot,
            table_id=table_id,
            table_name="Mesa T4",
            join_code="0000",  # wrong code
            org_id=org_id,
            location_id=location_id,
        )

    assert result is None, (
        "db_link_participant_session must return None when the code doesn't match."
    )


@pytest.mark.asyncio
async def test_wrong_code_increments_attempts(monkeypatch):
    """_handle_join_code_flow increments attempts counter on wrong code.

    Uses mocked state_store and db_link_participant_session to isolate the
    logic without needing a live DB for the flow handler itself.
    """
    from app.services import state_store, agent as agent_module

    phone = "573001110100"
    bot = "57999888001"
    pending = {
        "table_id": "tbl-test",
        "table_name": "Mesa Test",
        "attempts": 0,
        "org_id": 1,
        "location_id": None,
    }

    stored: dict = {}

    async def fake_set(ph, bn, state, ttl_seconds=600):
        stored.update(state)

    async def fake_delete(ph, bn):
        stored.clear()

    monkeypatch.setattr(state_store, "join_code_pending_set", fake_set)
    monkeypatch.setattr(state_store, "join_code_pending_delete", fake_delete)

    # Patch db_link_participant_session to always return None (wrong code).
    import app.repositories.tables_repo as tr_module

    async def fake_link(**kwargs):
        return None

    monkeypatch.setattr(tr_module, "db_link_participant_session", fake_link)

    result = await agent_module._handle_join_code_flow(
        phone, bot, "0000", pending
    )

    assert "incorrecto" in result["message"].lower() or "incorrect" in result["message"].lower(), (
        f"Expected 'incorrecto' in response, got: {result['message']!r}"
    )
    assert stored.get("attempts") == 1, (
        f"Expected attempts=1 after one wrong code, got {stored.get('attempts')!r}"
    )


@pytest.mark.asyncio
async def test_3_wrong_codes_blocks_phone(monkeypatch):
    """After 3 wrong codes, _handle_join_code_flow deletes state (blocks the phone)
    and returns a 'too many attempts' message. No more retries are accepted."""
    from app.services import state_store, agent as agent_module

    phone = "573001110101"
    bot = "57999888002"
    pending = {
        "table_id": "tbl-test",
        "table_name": "Mesa Test",
        "attempts": 2,  # already 2 failures — this is the 3rd
        "org_id": 1,
        "location_id": None,
    }

    deleted = []

    async def fake_set(ph, bn, state, ttl_seconds=600):
        pass

    async def fake_delete(ph, bn):
        deleted.append(True)

    monkeypatch.setattr(state_store, "join_code_pending_set", fake_set)
    monkeypatch.setattr(state_store, "join_code_pending_delete", fake_delete)

    import app.repositories.tables_repo as tr_module

    async def fake_link(**kwargs):
        return None

    monkeypatch.setattr(tr_module, "db_link_participant_session", fake_link)

    result = await agent_module._handle_join_code_flow(
        phone, bot, "1111", pending
    )

    assert len(deleted) == 1, "State must be deleted after 3 wrong attempts"
    assert "intento" in result["message"].lower() or "mesero" in result["message"].lower(), (
        f"Expected block message, got: {result['message']!r}"
    )


@_needs_db
@pytest.mark.asyncio
async def test_same_phone_rescan_ok_no_code(db_conn, org_id, location_id):
    """Phone A re-scans the same table it already has a session on.
    db_get_active_session_on_table_by_other_phone returns None (same phone
    is excluded), so no join_code is requested."""
    from app.repositories.tables_repo import db_get_active_session_on_table_by_other_phone

    bot = "57999000005"
    phone_a = "573001110010"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T5")

    with tenant_scope(org_id):
        await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T5",
            org_id, location_id, join_code="5555",
        )

        # Same phone rescans — should NOT see itself as "other".
        other = await db_get_active_session_on_table_by_other_phone(table_id, phone_a)
        assert other is None, (
            "db_get_active_session_on_table_by_other_phone must return None for same phone "
            "(same phone re-scan should be allowed without asking for a code)."
        )


@_needs_db
@pytest.mark.asyncio
async def test_phone_a_scans_different_table_closes_old(db_conn, org_id, location_id):
    """Phone A scans a DIFFERENT table than the one it already has a session on.
    db_close_session closes the old session and db_create_table_session opens the new one."""
    from app.repositories.tables_repo import (
        db_close_session,
        db_create_table_session,
        db_get_active_session,
    )

    bot = "57999000006"
    phone_a = "573001110020"
    table_id_1 = await _make_table(db_conn, org_id, location_id, suffix="T6a")
    table_id_2 = await _make_table(db_conn, org_id, location_id, suffix="T6b")

    with tenant_scope(org_id):
        # Phone A starts at table 1.
        await _make_session(
            db_conn, phone_a, bot, table_id_1, "Mesa T6a",
            org_id, location_id, join_code="1111",
        )

        session_before = await db_get_active_session(phone_a, bot)
        assert session_before["table_id"] == table_id_1

        # Phone A scans table 2 → old session should be closed, new one opened.
        await db_close_session(
            phone_a, bot, reason="scanned_new_table_via_qr_claim", closed_by_username="system"
        )
        new_session = await db_create_table_session(
            phone_a, bot, table_id_2, "Mesa T6b",
            org_id=org_id, location_id=location_id,
        )

        session_after = await db_get_active_session(phone_a, bot)
        assert session_after is not None
        assert session_after["table_id"] == table_id_2, (
            f"Expected active session at table2={table_id_2!r}, "
            f"got {session_after['table_id']!r}"
        )


@_needs_db
@pytest.mark.asyncio
async def test_join_code_persists_across_session_lifetime(db_conn, org_id, location_id):
    """join_code on a session does NOT change while the session is active.
    db_set_session_join_code returns False if a code is already set."""
    from app.repositories.tables_repo import db_set_session_join_code

    bot = "57999000007"
    phone_a = "573001110030"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T7")

    with tenant_scope(org_id):
        session = await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T7",
            org_id, location_id, join_code="7777",
        )

        # Attempt to overwrite the code — must fail (WHERE join_code IS NULL guard).
        updated = await db_set_session_join_code(session["id"], "8888")
        assert updated is False, (
            "db_set_session_join_code must return False when code is already set "
            "(prevents race overwrite)."
        )

        # Code in DB is still the original.
        stored = await db_conn.fetchval(
            "SELECT join_code FROM table_sessions WHERE id = $1",
            session["id"],
        )
        assert stored == "7777", f"join_code changed unexpectedly to {stored!r}"


@_needs_db
@pytest.mark.asyncio
async def test_join_code_cleared_on_session_close(db_conn, org_id, location_id):
    """After db_close_session, the next host that opens a session on the same table
    gets a NEW join_code (the old session is gone so db_get_session_join_code returns None)."""
    from app.repositories.tables_repo import (
        db_close_session,
        db_create_table_session,
        db_get_session_join_code,
        db_set_session_join_code,
    )
    from app.services.agent import _generate_join_code

    bot = "57999000008"
    phone_a = "573001110040"
    phone_b = "573001110041"
    table_id = await _make_table(db_conn, org_id, location_id, suffix="T8")

    with tenant_scope(org_id):
        # First host opens a session with a code.
        old_session = await _make_session(
            db_conn, phone_a, bot, table_id, "Mesa T8",
            org_id, location_id, join_code="3333",
        )

        # Close the session (simulate bill paid).
        await db_close_session(
            phone_a, bot, reason="factura_entregada", closed_by_username="system"
        )

        # The old join_code is gone (no active session).
        code_after_close = await db_get_session_join_code(table_id, bot)
        assert code_after_close is None, (
            "After session is closed, db_get_session_join_code must return None "
            "so the next host gets a fresh code."
        )

        # New host (phone B) opens a new session.
        new_session = await db_create_table_session(
            phone_b, bot, table_id, "Mesa T8",
            org_id=org_id, location_id=location_id,
        )
        new_code = _generate_join_code()
        updated = await db_set_session_join_code(new_session["id"], new_code)
        assert updated is True

        # New code is different from the old one (with high probability — both are random).
        # We just verify it's 4 digits and set.
        stored = await db_get_session_join_code(table_id, bot)
        assert stored == new_code
        assert re.fullmatch(r"\d{4}", stored)
