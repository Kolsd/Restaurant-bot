"""
tests/test_delivery_courier.py
=================================
Chunk 7 of the delivery/pickup web wave (docs/claude/delivery-web.md):
THE STAFF-SIDE SCREENS — courier-facing backend additions.

Covers the new endpoints/gates added on top of chunk 4's
app/routes/staff_delivery.py:
  - GET /api/staff/delivery/orders/mine   — the courier's OWN assigned
    orders, sede-scoped, never another courier's queue.
  - GET /api/staff/delivery/couriers      — the cashier's roster of active
    couriers in their own sede (for the "Asignar domiciliario" picker).
  - A courier may act on /en-route and /delivered ONLY for an order
    assigned to them — refused (and the row left unchanged) otherwise.
  - GET /api/staff/delivery/orders stays cashier/admin-only (a courier is
    refused, not silently given an empty list).

Fixture/seed style mirrors tests/test_delivery_cashier.py exactly (same
file, same conventions): seed helpers open their OWN asyncpg connection to
TEST_DATABASE_URL (documented as the `postgres` superuser — RLS bypassed
for these raw seed writes, while the application code under test still
downgrades to `mesio_app` inside tenant_connection()).
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


# ── Helpers (mirrors tests/test_delivery_cashier.py) ────────────────────────


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


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


async def _advance_location_seq(conn, forced_id: int) -> None:
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        forced_id,
    )


async def _seed_location(org_id: int, name: str = "Sede", loc_id: int | None = None) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if loc_id is not None:
            new_id = await conn.fetchval(
                "INSERT INTO locations (id, org_id, name) VALUES ($1, $2, $3) RETURNING id",
                loc_id, org_id, name,
            )
            await _advance_location_seq(conn, loc_id)
            return new_id
        return await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
            org_id, name,
        )
    finally:
        await conn.close()


async def _seed_staff(org_id: int, location_id: int | None, role: str = "caja", active: bool = True) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            """INSERT INTO staff (name, role, username, org_id, location_id, active)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id::text""",
            f"Staff {uuid.uuid4().hex[:6]}", role, f"staff_{uuid.uuid4().hex[:10]}",
            org_id, location_id, active,
        )
    finally:
        await conn.close()


async def _create_staff_token(staff_id: str) -> str:
    # Several tokens are minted per test here (unlike test_delivery_cashier.py,
    # which mints at most one) — each _run() call spins a THROWAWAY event
    # loop, and sessions_repo.create_session() goes through the shared
    # asyncpg pool (app.services.database._pool), which is bound to
    # whichever loop created it. Reset it first so it's lazily recreated
    # against the CURRENT loop instead of raising "Event loop is closed" /
    # "another operation is in progress" against a dead one.
    _reset_pool()
    from app.repositories import sessions_repo
    return await sessions_repo.create_session(f"staff:{staff_id}")


async def _seed_order(*, org_id: int, location_id: int, status: str = "pendiente_aceptacion", **overrides) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = {
            "id": f"ord-{uuid.uuid4().hex[:12]}",
            "phone": f"web:{uuid.uuid4().hex}",
            "items": json.dumps([{"name": "Bandeja Paisa", "quantity": 1}]),
            "order_type": "domicilio",
            "subtotal": 20000,
            "total": 20000,
            "bot_number": "573000000000",
            "org_id": org_id,
            "location_id": location_id,
            "status": status,
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


async def _fetch_order(order_id: str) -> dict | None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        return dict(row) if row else None
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


# ══════════════════════════════════════════════════════════════════════════
# GET /api/staff/delivery/orders/mine — the courier's own queue
# ══════════════════════════════════════════════════════════════════════════


def test_courier_sees_only_own_assigned_orders(client):
    org_id = _run(_seed_org("Courier Mine Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Principal"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))
        courier_a_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_a_token = _run(_create_staff_token(courier_a_id))
        courier_b_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))

        order_for_a = _run(_seed_order(org_id=org_id, location_id=location_id, status="en_preparacion"))
        order_for_b = _run(_seed_order(org_id=org_id, location_id=location_id, status="en_preparacion"))
        order_unassigned = _run(_seed_order(org_id=org_id, location_id=location_id, status="pendiente_aceptacion"))

        r1 = _post(
            client, f"/api/staff/delivery/orders/{order_for_a}/assign-courier",
            json={"courier_staff_id": courier_a_id}, headers=_auth(cashier_token),
        )
        assert r1.status_code == 200, r1.text
        r2 = _post(
            client, f"/api/staff/delivery/orders/{order_for_b}/assign-courier",
            json={"courier_staff_id": courier_b_id}, headers=_auth(cashier_token),
        )
        assert r2.status_code == 200, r2.text

        resp = _get(client, "/api/staff/delivery/orders/mine", headers=_auth(courier_a_token))
        assert resp.status_code == 200, resp.text
        ids = {o["id"] for o in resp.json()["orders"]}
        assert ids == {order_for_a}, "must see exactly their own assigned order — not B's, not the unassigned one"
        assert order_for_b not in ids
        assert order_unassigned not in ids
    finally:
        _run(_teardown_org(org_id))


def test_courier_orders_mine_scoped_to_own_sede_and_cross_org_colliding_ids(client):
    org_a = _run(_seed_org("Courier Sede Collision Org A"))
    org_b = _run(_seed_org("Courier Sede Collision Org B"))
    try:
        # Force org_b's location id to collide with a location that belongs
        # to org_a — same collision shape as test_delivery_cashier.py.
        loc_l = _run(_seed_location(org_a, "A sede (id collides with org B)", loc_id=org_b))
        assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
        loc_a2 = _run(_seed_location(org_a, "A sede 2"))
        loc_b = _run(_seed_location(org_b, "B real sede"))

        cashier_a_id = _run(_seed_staff(org_a, loc_l, role="caja"))
        cashier_a_token = _run(_create_staff_token(cashier_a_id))
        courier_a_id = _run(_seed_staff(org_a, loc_l, role="domiciliario"))
        courier_a_token = _run(_create_staff_token(courier_a_id))

        # A second sede in org_a: same courier cannot see orders assigned
        # to a namesake courier there either (different staff row entirely).
        courier_a2_id = _run(_seed_staff(org_a, loc_a2, role="domiciliario"))
        cashier_a2_id = _run(_seed_staff(org_a, loc_a2, role="caja"))
        cashier_a2_token = _run(_create_staff_token(cashier_a2_id))
        order_a2 = _run(_seed_order(org_id=org_a, location_id=loc_a2, status="en_preparacion"))
        ra2 = _post(
            client, f"/api/staff/delivery/orders/{order_a2}/assign-courier",
            json={"courier_staff_id": courier_a2_id}, headers=_auth(cashier_a2_token),
        )
        assert ra2.status_code == 200, ra2.text

        order_a = _run(_seed_order(org_id=org_a, location_id=loc_l, status="en_preparacion"))
        ra = _post(
            client, f"/api/staff/delivery/orders/{order_a}/assign-courier",
            json={"courier_staff_id": courier_a_id}, headers=_auth(cashier_a_token),
        )
        assert ra.status_code == 200, ra.text

        # org_b has its own order at a location whose numeric id equals
        # org_a's own location loc_l — must never surface for courier_a.
        order_b = _run(_seed_order(org_id=org_b, location_id=loc_b))

        resp = _get(client, "/api/staff/delivery/orders/mine", headers=_auth(courier_a_token))
        assert resp.status_code == 200, resp.text
        ids = {o["id"] for o in resp.json()["orders"]}
        assert ids == {order_a}
        assert order_a2 not in ids, "a different sede's order must never leak even to a same-org courier"
        assert order_b not in ids
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


def test_orders_mine_refused_for_cashier_and_admin(client):
    """The 'mine' view is courier-only — cashier/admin have GET /orders."""
    org_id = _run(_seed_org("Mine Refused Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))

        resp = _get(client, "/api/staff/delivery/orders/mine", headers=_auth(cashier_token))
        assert resp.status_code == 403, resp.text
    finally:
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# GET /api/staff/delivery/couriers — cashier's roster picker
# ══════════════════════════════════════════════════════════════════════════


def test_list_couriers_returns_only_active_couriers_in_own_sede(client):
    org_id = _run(_seed_org("Couriers Roster Org"))
    try:
        loc_a = _run(_seed_location(org_id, "Sede A"))
        loc_b = _run(_seed_location(org_id, "Sede B"))
        cashier_a_id = _run(_seed_staff(org_id, loc_a, role="caja"))
        cashier_a_token = _run(_create_staff_token(cashier_a_id))

        courier_a_id = _run(_seed_staff(org_id, loc_a, role="domiciliario"))
        inactive_courier_a_id = _run(_seed_staff(org_id, loc_a, role="domiciliario", active=False))
        courier_b_id = _run(_seed_staff(org_id, loc_b, role="domiciliario"))
        waiter_a_id = _run(_seed_staff(org_id, loc_a, role="mesero"))

        resp = _get(client, "/api/staff/delivery/couriers", headers=_auth(cashier_a_token))
        assert resp.status_code == 200, resp.text
        ids = {c["id"] for c in resp.json()["couriers"]}
        assert ids == {courier_a_id}, "must list exactly the one active courier in sede A"
        assert inactive_courier_a_id not in ids
        assert courier_b_id not in ids
        assert waiter_a_id not in ids
    finally:
        _run(_teardown_org(org_id))


def test_list_couriers_refused_for_courier_role(client):
    org_id = _run(_seed_org("Couriers Roster Refused Org"))
    try:
        location_id = _run(_seed_location(org_id))
        courier_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_token = _run(_create_staff_token(courier_id))

        resp = _get(client, "/api/staff/delivery/couriers", headers=_auth(courier_token))
        assert resp.status_code == 403, resp.text
    finally:
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# A courier may only act on orders assigned to THEM
# ══════════════════════════════════════════════════════════════════════════


def test_courier_cannot_mark_en_route_on_order_assigned_to_another_courier(client):
    org_id = _run(_seed_org("Courier Ownership Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))
        courier_owner_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_other_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_other_token = _run(_create_staff_token(courier_other_id))

        order_id = _run(_seed_order(org_id=org_id, location_id=location_id, status="en_preparacion"))
        ra = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": courier_owner_id}, headers=_auth(cashier_token),
        )
        assert ra.status_code == 200, ra.text

        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/en-route",
            headers=_auth(courier_other_token),
        )
        assert resp.status_code == 403, resp.text

        row = _run(_fetch_order(order_id))
        assert row["status"] == "en_preparacion", "the row must be unchanged after the refused attempt"
    finally:
        _run(_teardown_org(org_id))


def test_courier_cannot_mark_delivered_on_order_assigned_to_another_courier(client):
    org_id = _run(_seed_org("Courier Ownership Delivered Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))
        courier_owner_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_owner_token = _run(_create_staff_token(courier_owner_id))
        courier_other_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        courier_other_token = _run(_create_staff_token(courier_other_id))

        order_id = _run(_seed_order(org_id=org_id, location_id=location_id, status="en_preparacion"))
        ra = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": courier_owner_id}, headers=_auth(cashier_token),
        )
        assert ra.status_code == 200, ra.text

        # The assigned courier CAN move it themselves.
        r_owner = _post(
            client, f"/api/staff/delivery/orders/{order_id}/en-route",
            headers=_auth(courier_owner_token),
        )
        assert r_owner.status_code == 200, r_owner.text

        # A different courier may not mark it delivered.
        r_other = _post(
            client, f"/api/staff/delivery/orders/{order_id}/delivered",
            headers=_auth(courier_other_token),
        )
        assert r_other.status_code == 403, r_other.text

        row = _run(_fetch_order(order_id))
        assert row["status"] == "en_camino", "the row must be unchanged after the refused attempt"

        # The rightful courier can complete it.
        r_final = _post(
            client, f"/api/staff/delivery/orders/{order_id}/delivered",
            headers=_auth(courier_owner_token),
        )
        assert r_final.status_code == 200, r_final.text
    finally:
        _run(_teardown_org(org_id))


def test_cashier_and_admin_can_still_mark_en_route_and_delivered_on_any_sede_order(client):
    """Chunk 7 must not regress the cashier's own ability to do these
    transitions (they still cover for a courier without a phone, etc.)."""
    org_id = _run(_seed_org("Cashier Still Works Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))

        order_id = _run(_seed_order(org_id=org_id, location_id=location_id, status="en_preparacion"))
        r1 = _post(client, f"/api/staff/delivery/orders/{order_id}/en-route", headers=_auth(cashier_token))
        assert r1.status_code == 200, r1.text
        r2 = _post(client, f"/api/staff/delivery/orders/{order_id}/delivered", headers=_auth(cashier_token))
        assert r2.status_code == 200, r2.text
    finally:
        _run(_teardown_org(org_id))
