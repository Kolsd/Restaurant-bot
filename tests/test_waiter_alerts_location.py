"""
tests/test_waiter_alerts_location.py
======================================
Coverage for the Part 2 fixes to app/repositories/tables_repo.py and
app/routes/tables.py:

  1. `db_create_waiter_alert` / `db_get_waiter_alerts` now carry/filter by
     `location_id` — a multi-location restaurant (one org) must
     not leak location-2 alerts onto location-1's waiter screen. Legacy rows
     with location_id IS NULL (created before this column was populated)
     must still show up everywhere (never silently vanish).
  2. POST /api/waiter-alerts/{id}/dismiss is no longer a cross-tenant IDOR:
     an id belonging to another org must 404 and leave the row untouched.
  3. POST /api/waiter-alerts/admin-call no longer trusts the request body's
     tenant key — it resolves the caller's own restaurant server-side, so
     a body naming another restaurant cannot land an alert
     there, and the created row carries the CALLER's org_id.

Mirrors tests/test_diner_routes.py's real-DB seed/teardown + _run/_get/_post
event-loop workaround pattern.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _run(coro):
    """Run a coroutine on a fresh, throwaway event loop.

    Resets app.services.database._pool FIRST every time: this file makes
    MULTIPLE separate _run() calls within a single test (seed alerts, then
    read them back), each on its OWN throwaway loop. Without resetting
    between them, the second call would reuse a pool bound to the first
    call's now-closed loop (asyncpg raises "another operation is in
    progress" / "Event loop is closed") — see
    tests/test_diner_routes.py's module docstring, event-loop note 2, for
    the same root cause via TestClient instead of raw _run().
    """
    _reset_pool()
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


# ── Seed helpers ─────────────────────────────────────────────────────────────

async def _make_org_with_locations(conn, n_locations: int = 1) -> dict:
    suffix = uuid.uuid4().hex[:10]
    org_id = await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu, features) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
        f"Alerts Test Org {suffix}", f"alerts-test-{suffix}", "{}", "{}",
    )
    location_ids = []
    for i in range(n_locations):
        loc_id = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
            org_id, f"Sede {suffix}-{i}",
        )
        location_ids.append(loc_id)
    return {"org_id": org_id, "location_ids": location_ids}


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM waiter_alerts WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def two_location_org():
    """One org, two locations (A, B) — the exact scenario the bug report
    describes."""
    async def _seed():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await _make_org_with_locations(conn, n_locations=2)
        finally:
            await conn.close()

    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


@pytest.fixture
def org_a():
    info = _run(_make_org_via_new_conn())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


@pytest.fixture
def org_b():
    info = _run(_make_org_via_new_conn())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


async def _make_org_via_new_conn() -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await _make_org_with_locations(conn, n_locations=1)
    finally:
        await conn.close()


async def _resolve_org_id_for_location(location_id: int) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT org_id FROM locations WHERE id=$1", location_id)
    finally:
        await conn.close()


def _auth_as_location(monkeypatch, location_id: int, username: str = "owner_test"):
    """Make `require_auth`/`get_current_user`/`get_current_restaurant`
    resolve to a real seeded Location — WITHOUT mocking db_get_restaurant_by_org_id
    / db_get_restaurant_by_location_id themselves, so the real RLS-backed
    lookup (org_id/location_id) is exercised.

    P0 fix (2026-09): get_current_restaurant now REQUIRES the explicit
    org_id field on the user dict (backfilled onto users.org_id by the
    users_org_location migration) — it no longer resolves org_id from
    branch_id at all. We resolve the real org_id for the seeded location
    here (one extra real DB round-trip) so this mock matches the shape a
    genuinely backfilled user row has post-migration.
    """
    from app.services import database as db

    org_id = _run(_resolve_org_id_for_location(location_id))

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": location_id,
            "org_id": org_id, "location_id": location_id,
            "role": "owner", "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


# ── Repo-level: location filter + legacy NULL passthrough ───────────────────

async def _seed_three_alerts(org_id: int, loc_a: int, loc_b: int) -> None:
    from app.services.tenant_context import tenant_scope
    from app.repositories import tables_repo as tr

    with tenant_scope(org_id):
        await tr.db_create_waiter_alert(
            phone="web:a", org_id=org_id, alert_type="other",
            message="alert for A", table_id="ta", table_name="Mesa A",
            location_id=loc_a,
        )
        await tr.db_create_waiter_alert(
            phone="web:b", org_id=org_id, alert_type="other",
            message="alert for B", table_id="tb", table_name="Mesa B",
            location_id=loc_b,
        )
        await tr.db_create_waiter_alert(
            phone="web:legacy", org_id=org_id, alert_type="other",
            message="legacy alert, no location", table_id="tl", table_name="Mesa Legacy",
            location_id=None,
        )


def test_location_reader_sees_own_alert_and_legacy_not_other_location(two_location_org):
    from app.services.tenant_context import tenant_scope
    from app.repositories import tables_repo as tr

    org_id = two_location_org["org_id"]
    loc_a, loc_b = two_location_org["location_ids"]

    _run(_seed_three_alerts(org_id, loc_a, loc_b))

    async def _read_for(location_id):
        with tenant_scope(org_id):
            return await tr.db_get_waiter_alerts(org_id, location_id=location_id)

    alerts_a = _run(_read_for(loc_a))
    messages_a = {a["message"] for a in alerts_a}
    assert "alert for A" in messages_a
    assert "legacy alert, no location" in messages_a
    assert "alert for B" not in messages_a

    alerts_b = _run(_read_for(loc_b))
    messages_b = {a["message"] for a in alerts_b}
    assert "alert for B" in messages_b
    assert "legacy alert, no location" in messages_b
    assert "alert for A" not in messages_b


def test_no_location_filter_returns_every_sede(two_location_org):
    from app.services.tenant_context import tenant_scope
    from app.repositories import tables_repo as tr

    org_id = two_location_org["org_id"]
    loc_a, loc_b = two_location_org["location_ids"]
    _run(_seed_three_alerts(org_id, loc_a, loc_b))

    async def _read_all():
        with tenant_scope(org_id):
            return await tr.db_get_waiter_alerts(org_id, location_id=None)

    alerts = _run(_read_all())
    assert len(alerts) == 3


# ── Route-level: GET /api/waiter-alerts respects X-Branch-ID ────────────────

def test_get_waiter_alerts_filters_by_branch_header(client, two_location_org, monkeypatch):
    org_id = two_location_org["org_id"]
    loc_a, loc_b = two_location_org["location_ids"]
    _run(_seed_three_alerts(org_id, loc_a, loc_b))


    _auth_as_location(monkeypatch, loc_a)
    headers = dict(_AUTH_HEADERS)
    headers["X-Branch-ID"] = str(loc_a)
    resp = _get(client, "/api/waiter-alerts", headers=headers)
    assert resp.status_code == 200
    messages = {a["message"] for a in resp.json()["alerts"]}
    assert "alert for A" in messages
    assert "legacy alert, no location" in messages
    assert "alert for B" not in messages


def test_get_waiter_alerts_without_header_returns_all_locations(client, two_location_org, monkeypatch):
    org_id = two_location_org["org_id"]
    loc_a, loc_b = two_location_org["location_ids"]
    _run(_seed_three_alerts(org_id, loc_a, loc_b))

    _auth_as_location(monkeypatch, loc_a)
    resp = _get(client, "/api/waiter-alerts", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    # No X-Branch-ID header — today's behaviour is preserved: no location
    # filter, every sede of the org comes back.
    assert len(resp.json()["alerts"]) == 3


# ── Security fix 1: dismiss is no longer a cross-tenant IDOR ────────────────

def test_dismiss_foreign_org_alert_returns_404_and_row_untouched(client, org_a, org_b, monkeypatch):
    from app.services.tenant_context import tenant_scope
    from app.repositories import tables_repo as tr

    async def _create_alert_for_a():
        with tenant_scope(org_a["org_id"]):
            return await tr.db_create_waiter_alert(
                phone="web:victim", org_id=org_a["org_id"], alert_type="bill",
                message="org A's bill request", table_id="t1", table_name="Mesa 1",
                location_id=org_a["location_ids"][0],
            )

    alert = _run(_create_alert_for_a())
    alert_id = alert["id"]

    # Authenticate as ORG B's owner and try to dismiss ORG A's alert id.
    _auth_as_location(monkeypatch, org_b["location_ids"][0])
    resp = _post(client, f"/api/waiter-alerts/{alert_id}/dismiss", headers=_AUTH_HEADERS)
    assert resp.status_code == 404

    async def _reread():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await conn.fetchrow("SELECT dismissed FROM waiter_alerts WHERE id=$1", alert_id)
        finally:
            await conn.close()

    row = _run(_reread())
    assert row["dismissed"] is False


def test_dismiss_own_org_alert_succeeds(client, org_a, monkeypatch):
    from app.services.tenant_context import tenant_scope
    from app.repositories import tables_repo as tr

    async def _create_alert_for_a():
        with tenant_scope(org_a["org_id"]):
            return await tr.db_create_waiter_alert(
                phone="web:owner", org_id=org_a["org_id"], alert_type="bill",
                message="org A's own bill request", table_id="t1", table_name="Mesa 1",
                location_id=org_a["location_ids"][0],
            )

    alert = _run(_create_alert_for_a())
    alert_id = alert["id"]

    _auth_as_location(monkeypatch, org_a["location_ids"][0])
    resp = _post(client, f"/api/waiter-alerts/{alert_id}/dismiss", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ── Security fix 2: admin-call ignores any tenant key in the body ───────────

def test_admin_call_cannot_target_another_restaurant_via_body(client, org_a, org_b, monkeypatch):
    _auth_as_location(monkeypatch, org_a["location_ids"][0])

    resp = _post(
        client, "/api/waiter-alerts/admin-call",
        headers=_AUTH_HEADERS,
        json={"org_id": org_b["org_id"], "table_name": "Mesa hostil"},
    )
    assert resp.status_code == 200

    async def _counts():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            a_rows = await conn.fetch("SELECT * FROM waiter_alerts WHERE org_id=$1", org_a["org_id"])
            b_rows = await conn.fetch("SELECT * FROM waiter_alerts WHERE org_id=$1", org_b["org_id"])
            return a_rows, b_rows
        finally:
            await conn.close()

    a_rows, b_rows = _run(_counts())
    assert len(b_rows) == 0, "the alert must NOT land on org B despite the body naming org B"
    assert len(a_rows) == 1
    assert a_rows[0]["org_id"] == org_a["org_id"]
