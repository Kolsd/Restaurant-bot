"""
tests/test_kitchen_delivery_location_scope.py
================================================
Chunk 8 of the delivery/pickup web wave (docs/claude/delivery-web.md), part C
— THE KITCHEN DELIVERY FEED, scoped by sede:

  GET   /api/kitchen/delivery-orders                    — was org-scoped only;
  PATCH /api/kitchen/delivery-orders/{id}/status         — same gap on writes.

Before this chunk, every kitchen of a multi-sede org saw (and could act on)
every sede's delivery tickets — same gap class as the waiter alerts
(memory: mesero-location-gap). Staff JWTs carry location_id (chunk 4); this
closes the read AND the write side, following the X-Location-ID /
own-staff.location_id convention app/routes/staff_delivery.py::delivery_scope
already uses.

Real database, real HTTP (TestClient). Seed/auth conventions mirror
tests/test_delivery_cashier.py (staff JWT via sessions_repo.create_session)
and tests/test_waiter_alerts_location.py (admin auth via monkeypatched
verify_token/db_get_user).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


def _patch(client, url, **kwargs):
    _reset_pool()
    return client.patch(url, **kwargs)


_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _auth_as_admin(monkeypatch, org_id: int, username: str = "kitchen_scope_admin"):
    from app.services import database as db

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": None,
            "org_id": org_id, "location_id": None,
            "role": "owner", "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


async def _seed_org(name: str) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:8]
        return await conn.fetchval(
            "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
            name, f"{name.lower().replace(' ', '-')}-{suffix}",
        )
    finally:
        await conn.close()


async def _seed_location(org_id: int, name: str = "Sede") -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            "INSERT INTO locations (org_id, name, active) VALUES ($1, $2, true) RETURNING id",
            org_id, name,
        )
    finally:
        await conn.close()


async def _seed_staff(org_id: int, location_id: int | None, role: str = "cocina") -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            """INSERT INTO staff (name, role, username, org_id, location_id, active)
               VALUES ($1, $2, $3, $4, $5, true) RETURNING id::text""",
            f"Cook {uuid.uuid4().hex[:6]}", role, f"cook_{uuid.uuid4().hex[:10]}",
            org_id, location_id,
        )
    finally:
        await conn.close()


async def _create_staff_token(staff_id: str) -> str:
    from app.repositories import sessions_repo
    return await sessions_repo.create_session(f"staff:{staff_id}")


async def _seed_order(
    *, org_id: int, location_id: int, status: str = "en_preparacion", channel: str = "web_chat", **overrides,
) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = {
            "id": f"kord-{uuid.uuid4().hex[:12]}",
            "phone": f"web:{uuid.uuid4().hex}",
            "items": json.dumps([{"name": "Bandeja Paisa", "quantity": 1}]),
            "order_type": "domicilio",
            "subtotal": 20000,
            "total": 20000,
            "org_id": org_id,
            "location_id": location_id,
            "status": status,
            "channel": channel,
        }
        row.update(overrides)
        if isinstance(row["items"], (list, tuple)):
            row["items"] = json.dumps(row["items"])
        cols = list(row.keys())
        placeholders = [f"${i}::jsonb" if c == "items" else f"${i}" for i, c in enumerate(cols, start=1)]
        sql = f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING id"
        return await conn.fetchval(sql, *[row[c] for c in cols])
    finally:
        await conn.close()


async def _fetch_order_status(order_id: str) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT status FROM orders WHERE id = $1", order_id)
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM orders WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM staff WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def two_sede_org():
    async def _seed():
        org_id = await _seed_org("Kitchen Scope Org")
        loc_a = await _seed_location(org_id, "Sede A")
        loc_b = await _seed_location(org_id, "Sede B")
        return {"org_id": org_id, "loc_a": loc_a, "loc_b": loc_b}
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


# ── GET /api/kitchen/delivery-orders ────────────────────────────────────


def test_cook_of_sede_a_never_sees_sede_b_tickets(client, two_sede_org):
    org_id = two_sede_org["org_id"]
    loc_a, loc_b = two_sede_org["loc_a"], two_sede_org["loc_b"]

    order_a = _run(_seed_order(org_id=org_id, location_id=loc_a))
    order_b = _run(_seed_order(org_id=org_id, location_id=loc_b))

    cook_a = _run(_seed_staff(org_id, loc_a, role="cocina"))
    token = _run(_create_staff_token(cook_a))

    resp = _get(client, "/api/kitchen/delivery-orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    ids = {o["id"] for o in resp.json()["orders"]}
    assert order_a in ids
    assert order_b not in ids


def test_cook_with_no_sede_assigned_is_refused(client, two_sede_org):
    org_id = two_sede_org["org_id"]
    loc_a = two_sede_org["loc_a"]
    _run(_seed_order(org_id=org_id, location_id=loc_a))

    unassigned_cook = _run(_seed_staff(org_id, None, role="cocina"))
    token = _run(_create_staff_token(unassigned_cook))

    resp = _get(client, "/api/kitchen/delivery-orders", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, resp.text


def test_admin_with_no_header_sees_every_sede(client, two_sede_org, monkeypatch):
    org_id = two_sede_org["org_id"]
    loc_a, loc_b = two_sede_org["loc_a"], two_sede_org["loc_b"]
    order_a = _run(_seed_order(org_id=org_id, location_id=loc_a))
    order_b = _run(_seed_order(org_id=org_id, location_id=loc_b))

    _auth_as_admin(monkeypatch, org_id)
    resp = _get(client, "/api/kitchen/delivery-orders", headers=_AUTH_HEADERS)
    assert resp.status_code == 200, resp.text
    ids = {o["id"] for o in resp.json()["orders"]}
    assert order_a in ids
    assert order_b in ids


def test_admin_with_header_scopes_to_one_sede(client, two_sede_org, monkeypatch):
    org_id = two_sede_org["org_id"]
    loc_a, loc_b = two_sede_org["loc_a"], two_sede_org["loc_b"]
    order_a = _run(_seed_order(org_id=org_id, location_id=loc_a))
    order_b = _run(_seed_order(org_id=org_id, location_id=loc_b))

    _auth_as_admin(monkeypatch, org_id, username="kitchen_scope_admin_header")
    headers = dict(_AUTH_HEADERS)
    headers["X-Location-ID"] = str(loc_a)
    resp = _get(client, "/api/kitchen/delivery-orders", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {o["id"] for o in resp.json()["orders"]}
    assert order_a in ids
    assert order_b not in ids


def test_cross_org_isolation_with_colliding_location_ids(client):
    """Deliberate id collision: org_b's own id equals a location that
    belongs to org_a — same shape used across this wave's other tests."""
    org_a = _run(_seed_org("Kitchen Scope Collision A"))
    org_b = _run(_seed_org("Kitchen Scope Collision B"))

    async def _seed_colliding_location(a: int, b: int) -> int:
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            loc_id = await conn.fetchval(
                "INSERT INTO locations (id, org_id, name, active) VALUES ($1, $2, $3, true) RETURNING id",
                b, a, "A's sede (collides with org B id)",
            )
            await conn.execute(
                "SELECT setval("
                "  pg_get_serial_sequence('locations', 'id'),"
                "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
                ")",
                loc_id,
            )
            return loc_id
        finally:
            await conn.close()

    loc_l = _run(_seed_colliding_location(org_a, org_b))
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
    try:
        order_a = _run(_seed_order(org_id=org_a, location_id=loc_l))
        cook_a = _run(_seed_staff(org_a, loc_l, role="cocina"))
        token = _run(_create_staff_token(cook_a))

        resp = _get(client, "/api/kitchen/delivery-orders", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        ids = {o["id"] for o in resp.json()["orders"]}
        assert order_a in ids
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


# ── PATCH /api/kitchen/delivery-orders/{id}/status ──────────────────────


def test_cook_cannot_mark_ready_another_sedes_order(client, two_sede_org):
    org_id = two_sede_org["org_id"]
    loc_a, loc_b = two_sede_org["loc_a"], two_sede_org["loc_b"]
    order_b = _run(_seed_order(org_id=org_id, location_id=loc_b, status="en_preparacion"))

    cook_a = _run(_seed_staff(org_id, loc_a, role="cocina"))
    token = _run(_create_staff_token(cook_a))

    resp = _patch(
        client, f"/api/kitchen/delivery-orders/{order_b}/status",
        headers={"Authorization": f"Bearer {token}"}, json={"status": "listo"},
    )
    assert resp.status_code == 404, resp.text

    status = _run(_fetch_order_status(order_b))
    assert status == "en_preparacion", "the row must be untouched by a refused cross-sede PATCH"


def test_cook_can_mark_ready_own_sedes_order(client, two_sede_org):
    org_id = two_sede_org["org_id"]
    loc_a = two_sede_org["loc_a"]
    order_a = _run(_seed_order(org_id=org_id, location_id=loc_a, status="en_preparacion"))

    cook_a = _run(_seed_staff(org_id, loc_a, role="cocina"))
    token = _run(_create_staff_token(cook_a))

    resp = _patch(
        client, f"/api/kitchen/delivery-orders/{order_a}/status",
        headers={"Authorization": f"Bearer {token}"}, json={"status": "listo"},
    )
    assert resp.status_code == 200, resp.text

    status = _run(_fetch_order_status(order_a))
    assert status == "listo"
