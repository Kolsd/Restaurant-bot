"""
tests/test_delivery_cashier.py
=================================
Chunk 4 of the delivery/pickup web wave (docs/claude/delivery-web.md):
THE CASHIER'S "DOMICILIOS" SURFACE — backend only.

Covers:
  A. The sold-out / inventory-deduction guard closed in this chunk
     (POST /api/diner/delivery/checkout, app/routes/diner_delivery.py).
  B. app.services.staff_sections — the `delivery` section grant.
  C. The new cashier endpoints (app/routes/staff_delivery.py): list, accept,
     reject, assign courier, en-route, delivered — real HTTP calls through
     TestClient against the real (rolled-back-free, explicitly torn down)
     test database, exactly like tests/test_delivery_checkout.py's
     HTTP-level section, because these routes depend on the REAL staff auth
     path (app/routes/deps.py::get_current_user) which this chunk also fixed
     — a mocked user dict would hide a regression in that fix.

Fixture/seed style mirrors tests/test_delivery_checkout.py and
tests/test_delivery_repo.py (same repo, same conventions):
  - Seed helpers open their OWN asyncpg connection to TEST_DATABASE_URL,
    which the documented convention (docs/claude/testing.md) runs as the
    `postgres` superuser — RLS is bypassed for these raw seed writes/reads
    (no app.org_id GUC needed), while the APPLICATION CODE under test still
    downgrades to `mesio_app` internally inside tenant_connection() (see
    app/services/tenant_db.py), so RLS is genuinely exercised for the code
    this file is actually testing.
  - A location seeded with a FORCED explicit id (the collision test) does
    NOT advance locations' BIGSERIAL sequence — _advance_location_seq fixes
    that up, exactly like test_delivery_entry.py / test_delivery_checkout.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone as dt_timezone

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── Helpers ──────────────────────────────────────────────────────────────


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


def _patch(client, url, **kwargs):
    _reset_pool()
    return client.patch(url, **kwargs)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _recent_utc(ts) -> bool:
    assert ts is not None, "expected a timestamp, got None"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_timezone.utc)
    delta = abs((datetime.now(dt_timezone.utc) - ts).total_seconds())
    return delta < 60


async def _advance_location_seq(conn, forced_id: int) -> None:
    """A forced explicit id does NOT advance locations' BIGSERIAL sequence —
    see tests/test_delivery_repo.py's helper of the same name."""
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        forced_id,
    )


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


async def _seed_inventory(org_id: int, dish_name: str, current_stock: float) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            """INSERT INTO inventory (org_id, name, unit, current_stock, min_stock, linked_dishes)
               VALUES ($1, $2, 'unidades', $3, 0, $4::jsonb) RETURNING id""",
            org_id, f"Insumo {dish_name}", current_stock, json.dumps([dish_name]),
        )
    finally:
        await conn.close()


async def _fetch_inventory(item_id: int) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM inventory WHERE id = $1", item_id)
        return dict(row)
    finally:
        await conn.close()


async def _seed_menu_availability(org_id: int, dish_name: str, available: bool,
                                  location_id: int | None = None) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, org_id, location_id, available)
               VALUES ($1, $2, $3, $4)""",
            dish_name, org_id, location_id, available,
        )
    finally:
        await conn.close()


async def _count_orders(org_id: int) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT COUNT(*) FROM orders WHERE org_id = $1", org_id)
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM orders WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM inventory WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM menu_availability WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM carts WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM staff WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


async def _seed_checkout_org(
    *, payment_methods=("efectivo", "nequi"), min_order=0, delivery_fee=3000,
) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        bot_number = f"573{suffix[:9]}"
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            f"Cashier Checkout Org {suffix}", f"cashier-checkout-{suffix}", json.dumps({"currency": "COP"}),
        )
        location_id = await conn.fetchval(
            """
            INSERT INTO locations
                (org_id, name, whatsapp_number, latitude, longitude, phone, address,
                 delivery_config, opening_hours)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
            RETURNING id
            """,
            org_id, f"Sede {suffix}", bot_number, 4.6097, -74.0817, "3011234567", "Cra 1 # 2-3",
            json.dumps({
                "delivery_enabled": True, "pickup_enabled": True,
                "delivery_fee": delivery_fee, "min_order": min_order, "radius_km": 50,
                "payment_methods": list(payment_methods),
            }),
            json.dumps({}),
        )
        return {"org_id": org_id, "location_id": location_id, "bot_number": bot_number, "suffix": suffix}
    finally:
        await conn.close()


async def _seed_session(org_id: int, location_id: int, bot_number: str) -> str:
    token = f"web:{uuid.uuid4()}"
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO diner_sessions (token, org_id, location_id, table_id, table_name, bot_number, order_mode)
               VALUES ($1, $2, $3, NULL, NULL, $4, 'delivery')""",
            token, org_id, location_id, bot_number,
        )
    finally:
        await conn.close()
    return token


async def _seed_cart(token: str, bot_number: str, org_id: int, items: list) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO carts (phone, bot_number, cart_data, updated_at, org_id)
               VALUES ($1, $2, $3::jsonb, NOW(), $4)
               ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data = EXCLUDED.cart_data""",
            token, bot_number, json.dumps({"items": items, "order_type": None, "address": None, "notes": ""}),
            org_id,
        )
    finally:
        await conn.close()


def _default_checkout_body(token: str, **overrides) -> dict:
    body = {
        "token": token,
        "idempotency_key": uuid.uuid4().hex,
        "customer_name": "Ana Pérez",
        "customer_phone": "3001112233",
        "address": "Calle 10 # 5-20, Barrio Centro",
        "lat": 4.6097,
        "lon": -74.0817,
        "payment_method": "efectivo",
        "cash_change_for": 100000.0,
        "tip_amount": 0.0,
    }
    body.update(overrides)
    return body


# ══════════════════════════════════════════════════════════════════════════
# A. Sold-out refusal + inventory-deduction decision (checkout route)
# ══════════════════════════════════════════════════════════════════════════


def test_checkout_refused_when_dish_marked_sold_out(client):
    info = _run(_seed_checkout_org())
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        # Sold out at THIS sede (per-sede since migration 0091).
        _run(_seed_menu_availability(info["org_id"], "Bandeja Paisa", False,
                                     location_id=info["location_id"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 40000.0, "line_id": "a1"},
        ]))

        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["reason"] == "dish_sold_out"
        assert "Bandeja Paisa" in detail["message"]

        assert _run(_count_orders(info["org_id"])) == 0, "a sold-out refusal must never create an order row"
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_refused_when_inventory_insufficient_and_creates_no_order(client):
    """The inventory-deduction decision for this chunk: a web delivery order
    DOES deduct inventory, atomically with the INSERT (see
    app/repositories/delivery_repo.py::db_create_delivery_order and
    orders_repo.deduct_inventory_in_tx). A shortage must refuse the
    checkout AND leave no orphaned order row behind — proven here by
    counting orders after the refusal, not just checking the status code."""
    info = _run(_seed_checkout_org())
    try:
        _run(_seed_inventory(info["org_id"], "Bandeja Paisa", current_stock=1))
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 2, "subtotal": 40000.0, "line_id": "a1"},
        ]))

        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "insufficient_stock"

        assert _run(_count_orders(info["org_id"])) == 0, (
            "an insufficient-stock refusal must roll back the INSERT too — no orphaned order row"
        )
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_success_actually_deducts_inventory(client):
    """The happy-path half of the same decision: a successful web delivery
    checkout must actually decrement stock, exactly like a table order."""
    info = _run(_seed_checkout_org())
    try:
        inv_id = _run(_seed_inventory(info["org_id"], "Bandeja Paisa", current_stock=10))
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 3, "subtotal": 60000.0, "line_id": "a1"},
        ]))

        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 200, resp.text

        inv_row = _run(_fetch_inventory(inv_id))
        assert inv_row["current_stock"] == 7, "3 units of Bandeja Paisa must have been deducted"
    finally:
        _run(_teardown_org(info["org_id"]))


# ══════════════════════════════════════════════════════════════════════════
# B. staff_sections — "delivery" section grant
# ══════════════════════════════════════════════════════════════════════════


def test_sections_for_roles_grants_delivery_to_cashier():
    from app.services.staff_sections import sections_for_roles
    for role in ("caja", "cashier", "cajero"):
        assert "delivery" in sections_for_roles([role]), f"role {role!r} must see Domicilios"


def test_sections_for_roles_grants_delivery_to_every_admin_role():
    from app.services.staff_sections import sections_for_roles
    for role in ("owner", "admin", "gerente"):
        assert "delivery" in sections_for_roles([role]), f"admin role {role!r} must see Domicilios"


def test_sections_for_roles_does_not_grant_delivery_to_waiter_cook_or_bar():
    from app.services.staff_sections import sections_for_roles
    assert "delivery" not in sections_for_roles(["mesero"])
    assert "delivery" not in sections_for_roles(["cocina"])
    assert "delivery" not in sections_for_roles(["bar"])


# ══════════════════════════════════════════════════════════════════════════
# C. Cashier endpoints — real HTTP calls, real staff auth, real DB rows
# ══════════════════════════════════════════════════════════════════════════


def test_cashier_full_happy_path_pending_to_delivered(client):
    org_id = _run(_seed_org("Cashier Happy Path Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Principal"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        cashier_token = _run(_create_staff_token(cashier_id))
        courier_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))

        order_id = _run(_seed_order(org_id=org_id, location_id=location_id))

        # Before acceptance: NOT visible to the kitchen KDS feed.
        from app.repositories import tables_repo
        from app.services.tenant_context import tenant_scope

        def _kitchen_feed():
            # A fresh pool each call — this helper hops between the
            # TestClient's own event loop (HTTP calls above/below) and the
            # throwaway loop _run() spins up; sharing an asyncpg pool across
            # loops raises "Future attached to a different loop".
            _reset_pool()
            with tenant_scope(org_id):
                return _run(tables_repo.db_get_delivery_orders_for_cashier())

        pre_accept_kitchen = _kitchen_feed()
        assert order_id not in {o["id"] for o in pre_accept_kitchen}, (
            "an unaccepted order must never reach the kitchen KDS feed"
        )

        # 1. Accept with an ETA.
        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/accept",
            json={"eta_minutes": 20}, headers=_auth(cashier_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "en_preparacion"
        assert body["accepted_by_staff_id"] == cashier_id
        assert body["estimated_minutes"] == 20
        assert _recent_utc(body["accepted_at"])

        # 2. NOW the kitchen KDS feed must show it — verified for real, not assumed.
        post_accept_kitchen = _kitchen_feed()
        assert order_id in {o["id"] for o in post_accept_kitchen}, (
            "accepting must release the ticket to the kitchen KDS feed"
        )

        # 3. Assign a courier.
        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": courier_id}, headers=_auth(cashier_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["courier_staff_id"] == courier_id
        assert _recent_utc(body["courier_assigned_at"])

        # 4. Move to en_camino.
        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/en-route",
            headers=_auth(cashier_token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "en_camino"

        # 5. Mark delivered.
        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/delivered",
            headers=_auth(cashier_token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "entregado"
        assert _recent_utc(body["delivered_at"])

        row = _run(_fetch_order(order_id))
        assert row["status"] == "entregado"
        assert row["accepted_by_staff_id"] is not None
        assert str(row["courier_staff_id"]) == courier_id
    finally:
        _run(_teardown_org(org_id))


def test_cashier_reject_requires_reason_and_sets_it(client):
    org_id = _run(_seed_org("Cashier Reject Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        order_id = _run(_seed_order(org_id=org_id, location_id=location_id))

        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/reject",
            json={"reason": "Sin repartidores disponibles"}, headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "rechazado"
        assert body["rejection_reason"] == "Sin repartidores disponibles"
    finally:
        _run(_teardown_org(org_id))


def test_cashier_illegal_transitions_are_refused(client):
    org_id = _run(_seed_org("Cashier Illegal Transitions Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))

        # Accept twice.
        order_1 = _run(_seed_order(org_id=org_id, location_id=location_id))
        r1 = _post(client, f"/api/staff/delivery/orders/{order_1}/accept", json={"eta_minutes": 10}, headers=_auth(token))
        assert r1.status_code == 200, r1.text
        r2 = _post(client, f"/api/staff/delivery/orders/{order_1}/accept", json={"eta_minutes": 10}, headers=_auth(token))
        assert r2.status_code == 409

        # Accept an already-rejected order.
        order_2 = _run(_seed_order(org_id=org_id, location_id=location_id))
        rr = _post(client, f"/api/staff/delivery/orders/{order_2}/reject", json={"reason": "motivo"}, headers=_auth(token))
        assert rr.status_code == 200, rr.text
        ra = _post(client, f"/api/staff/delivery/orders/{order_2}/accept", json={"eta_minutes": 10}, headers=_auth(token))
        assert ra.status_code == 409

        # Reject after acceptance.
        order_3 = _run(_seed_order(org_id=org_id, location_id=location_id))
        ac = _post(client, f"/api/staff/delivery/orders/{order_3}/accept", json={"eta_minutes": 10}, headers=_auth(token))
        assert ac.status_code == 200, ac.text
        rj = _post(client, f"/api/staff/delivery/orders/{order_3}/reject", json={"reason": "tarde"}, headers=_auth(token))
        assert rj.status_code == 409

        # Assign a courier to a delivered (terminal) order.
        order_4 = _run(_seed_order(org_id=org_id, location_id=location_id, status="entregado"))
        courier_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        rc = _post(
            client, f"/api/staff/delivery/orders/{order_4}/assign-courier",
            json={"courier_staff_id": courier_id}, headers=_auth(token),
        )
        assert rc.status_code == 409
    finally:
        _run(_teardown_org(org_id))


def test_cashier_cannot_act_on_another_sede_of_the_same_org(client):
    org_id = _run(_seed_org("Multi Sede Org"))
    try:
        loc_a = _run(_seed_location(org_id, "Sede A"))
        loc_b = _run(_seed_location(org_id, "Sede B"))
        cashier_a_id = _run(_seed_staff(org_id, loc_a, role="caja"))
        token_a = _run(_create_staff_token(cashier_a_id))

        order_b = _run(_seed_order(org_id=org_id, location_id=loc_b))

        # Listing must not surface sede B's order to a sede A cashier.
        resp = _get(client, "/api/staff/delivery/orders", headers=_auth(token_a))
        assert resp.status_code == 200, resp.text
        assert order_b not in {o["id"] for o in resp.json()["orders"]}

        # Nor may they accept / reject / assign-courier on it. A cross-sede
        # order is an AUTHORIZATION/VISIBILITY problem, not a state
        # conflict: it gets the same 404 "not found" treatment the rest of
        # this codebase gives a cross-tenant id (e.g. _resolve_org_or_404 in
        # app/routes/diner_delivery.py, get_current_location's unowned
        # lookup in app/routes/deps.py) — 409 stays reserved for a genuine
        # same-order state conflict (see test_cashier_illegal_transitions_
        # are_refused), matching the existing cart_lock_contention -> 409
        # convention. See delivery_repo.db_get_delivery_order_for_sede's
        # docstring for the full reasoning.
        r_accept = _post(client, f"/api/staff/delivery/orders/{order_b}/accept", json={"eta_minutes": 10}, headers=_auth(token_a))
        assert r_accept.status_code == 404
        r_reject = _post(client, f"/api/staff/delivery/orders/{order_b}/reject", json={"reason": "x"}, headers=_auth(token_a))
        assert r_reject.status_code == 404

        courier_b_id = _run(_seed_staff(org_id, loc_b, role="domiciliario"))
        r_assign = _post(
            client, f"/api/staff/delivery/orders/{order_b}/assign-courier",
            json={"courier_staff_id": courier_b_id}, headers=_auth(token_a),
        )
        assert r_assign.status_code == 404

        row = _run(_fetch_order(order_b))
        assert row["status"] == "pendiente_aceptacion", "sede A must never have touched sede B's order"
    finally:
        _run(_teardown_org(org_id))


def test_cashier_cross_org_isolation_with_colliding_ids(client):
    """org_b's own id is forced to equal a location that actually belongs to
    org_a — same collision shape as test_delivery_repo.py/test_delivery_checkout.py."""
    org_a = _run(_seed_org("Cashier Collision Org A"))
    org_b = _run(_seed_org("Cashier Collision Org B"))
    try:
        loc_l = _run(_seed_location(org_a, "A sede (id collides with org B)", loc_id=org_b))
        assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
        loc_b = _run(_seed_location(org_b, "B real sede"))

        order_a = _run(_seed_order(org_id=org_a, location_id=loc_l))

        cashier_b_id = _run(_seed_staff(org_b, loc_b, role="caja"))
        token_b = _run(_create_staff_token(cashier_b_id))

        # cashier_b's org filter is org_b; org_a's order must be invisible
        # and untouchable even though loc_l's numeric id equals org_b's id.
        resp = _get(client, "/api/staff/delivery/orders", headers=_auth(token_b))
        assert resp.status_code == 200, resp.text
        assert order_a not in {o["id"] for o in resp.json()["orders"]}

        # Cross-org is the same authorization/visibility problem as
        # cross-sede (see the comment in
        # test_cashier_cannot_act_on_another_sede_of_the_same_org) -> 404.
        r_accept = _post(client, f"/api/staff/delivery/orders/{order_a}/accept", json={"eta_minutes": 10}, headers=_auth(token_b))
        assert r_accept.status_code == 404

        row = _run(_fetch_order(order_a))
        assert row["status"] == "pendiente_aceptacion"
        assert row["org_id"] == org_a
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


def test_assign_courier_refused_for_other_org_other_sede_or_no_courier_role(client):
    org_id = _run(_seed_org("Courier Validation Org"))
    other_org_id = _run(_seed_org("Courier Other Org"))
    try:
        loc_a = _run(_seed_location(org_id, "Sede A"))
        loc_b = _run(_seed_location(org_id, "Sede B"))
        cashier_id = _run(_seed_staff(org_id, loc_a, role="caja"))
        token = _run(_create_staff_token(cashier_id))

        order_id = _run(_seed_order(org_id=org_id, location_id=loc_a))
        ac = _post(client, f"/api/staff/delivery/orders/{order_id}/accept", json={"eta_minutes": 10}, headers=_auth(token))
        assert ac.status_code == 200, ac.text

        # 1. Courier belongs to a DIFFERENT org.
        foreign_courier_id = _run(_seed_staff(other_org_id, None, role="domiciliario"))
        r1 = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": foreign_courier_id}, headers=_auth(token),
        )
        assert r1.status_code == 404

        # 2. Courier belongs to the SAME org but a DIFFERENT sede.
        wrong_sede_courier_id = _run(_seed_staff(org_id, loc_b, role="domiciliario"))
        r2 = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": wrong_sede_courier_id}, headers=_auth(token),
        )
        assert r2.status_code == 422

        # 3. Same org+sede, but no courier role.
        waiter_id = _run(_seed_staff(org_id, loc_a, role="mesero"))
        r3 = _post(
            client, f"/api/staff/delivery/orders/{order_id}/assign-courier",
            json={"courier_staff_id": waiter_id}, headers=_auth(token),
        )
        assert r3.status_code == 422

        row = _run(_fetch_order(order_id))
        assert row["courier_staff_id"] is None, "none of the three invalid assignments may have gone through"
    finally:
        _run(_teardown_org(org_id))
        _run(_teardown_org(other_org_id))


def test_cashier_endpoints_require_a_valid_session(client):
    resp = _get(client, "/api/staff/delivery/orders")
    assert resp.status_code == 401


def test_non_cashier_non_admin_role_is_refused(client):
    org_id = _run(_seed_org("Waiter Denied Org"))
    try:
        location_id = _run(_seed_location(org_id))
        waiter_id = _run(_seed_staff(org_id, location_id, role="mesero"))
        token = _run(_create_staff_token(waiter_id))

        resp = _get(client, "/api/staff/delivery/orders", headers=_auth(token))
        assert resp.status_code == 403
    finally:
        _run(_teardown_org(org_id))


# ── Kitchen "Listo" on a WEB order (PATCH /api/kitchen/delivery-orders/{id}/status) ──
# The legacy handler let the kitchen set any status, never told the
# customer's status page, and sent WhatsApp (+ the WhatsApp NPS) to the
# order's `phone` — a `web:<uuid>` identity for web orders.


def test_kitchen_marks_a_web_order_ready_without_whatsapp_and_notifies_the_customer(client):
    from unittest.mock import AsyncMock, patch

    org_id = _run(_seed_org("Kitchen Ready Org"))
    try:
        loc = _run(_seed_location(org_id))
        cook = _run(_seed_staff(org_id, loc, role="cocina"))
        token = _run(_create_staff_token(cook))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=loc, status="en_preparacion", channel="web_chat",
        ))

        with patch("app.routes.tables.send_wa_msg", new=AsyncMock()) as wa, \
             patch("app.routes.tables.trigger_nps", new=AsyncMock()) as nps, \
             patch("app.services.realtime.publish_delivery_status", new=AsyncMock()) as pub:
            resp = _patch(client, 
                f"/api/kitchen/delivery-orders/{order_id}/status",
                json={"status": "listo"}, headers=_auth(token),
            )

        assert resp.status_code == 200, resp.text
        assert _run(_fetch_order(order_id))["status"] == "listo"
        wa.assert_not_called()
        nps.assert_not_called()
        pub.assert_awaited_once()
        assert pub.await_args.args[2] == order_id, "the customer's page must be told about THIS order"
    finally:
        _run(_teardown_org(org_id))


def test_kitchen_cannot_move_a_web_order_to_any_other_status(client):
    org_id = _run(_seed_org("Kitchen Other Status Org"))
    try:
        loc = _run(_seed_location(org_id))
        token = _run(_create_staff_token(_run(_seed_staff(org_id, loc, role="cocina"))))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=loc, status="en_preparacion", channel="web_chat",
        ))
        for status in ("en_camino", "entregado", "cancelado", "confirmado"):
            resp = _patch(client, 
                f"/api/kitchen/delivery-orders/{order_id}/status",
                json={"status": status}, headers=_auth(token),
            )
            assert resp.status_code == 409, (status, resp.text)
        assert _run(_fetch_order(order_id))["status"] == "en_preparacion"
    finally:
        _run(_teardown_org(org_id))


def test_kitchen_cannot_mark_ready_before_the_cashier_accepts(client):
    org_id = _run(_seed_org("Kitchen Before Accept Org"))
    try:
        loc = _run(_seed_location(org_id))
        token = _run(_create_staff_token(_run(_seed_staff(org_id, loc, role="cocina"))))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=loc, status="pendiente_aceptacion", channel="web_chat",
        ))
        resp = _patch(client, 
            f"/api/kitchen/delivery-orders/{order_id}/status",
            json={"status": "listo"}, headers=_auth(token),
        )
        assert resp.status_code == 409, resp.text
        assert _run(_fetch_order(order_id))["status"] == "pendiente_aceptacion"
    finally:
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# Registering payment on a web order (POST .../mark-paid)
#
# Before this endpoint existed, `orders.paid` was written in exactly ONE
# place — orders_repo.db_confirm_payment, called only by the Wompi webhook,
# which is switched off. So no web delivery/pickup order could ever be paid:
# not cash at the door, not the rider's card reader, not a transfer whose
# receipt the cashier had already checked. stats_repo sums
# `orders.total WHERE paid = TRUE`, so every delivery sale was also missing
# from the owner's reports.
# ══════════════════════════════════════════════════════════════════════════

def test_cashier_registers_payment_and_the_row_records_who_and_how(client):
    org_id = _run(_seed_org("Pago Caja Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Pago"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id,
            status="en_preparacion", payment_method="nequi",
        ))

        before = _run(_fetch_order(order_id))
        assert before["paid"] is False, "seed must start unpaid"

        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "nequi"}, headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["paid"] is True
        assert body["payment_method"] == "nequi"
        assert body["paid_by_staff_id"] == cashier_id
        assert _recent_utc(body["paid_at"])

        # Verified in the DB, not only in the response.
        row = _run(_fetch_order(order_id))
        assert row["paid"] is True
        assert str(row["paid_by_staff_id"]) == cashier_id
        assert row["payment_method"] == "nequi"
        assert row["paid_at"] is not None
    finally:
        _run(_teardown_org(org_id))


def test_registering_payment_twice_is_refused_not_silently_overwritten(client):
    """The second submit must 409. Two tablets can hit this at once, and the
    loser must not overwrite who collected the money or when."""
    org_id = _run(_seed_org("Pago Doble Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Pago"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="en_preparacion",
        ))

        first = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "efectivo"}, headers=_auth(token),
        )
        assert first.status_code == 200, first.text
        first_paid_at = _run(_fetch_order(order_id))["paid_at"]

        second = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "tarjeta"}, headers=_auth(token),
        )
        assert second.status_code == 409, second.text

        row = _run(_fetch_order(order_id))
        assert row["payment_method"] == "efectivo", "the second call must not win"
        assert row["paid_at"] == first_paid_at
    finally:
        _run(_teardown_org(org_id))


def test_cancelled_order_cannot_be_registered_as_paid(client):
    """An order nobody will serve is not revenue."""
    org_id = _run(_seed_org("Pago Cancelado Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Pago"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        for status in ("cancelado", "rechazado"):
            order_id = _run(_seed_order(
                org_id=org_id, location_id=location_id, status=status,
            ))
            resp = _post(
                client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
                json={"payment_method": "efectivo"}, headers=_auth(token),
            )
            assert resp.status_code == 409, f"{status}: {resp.text}"
            assert _run(_fetch_order(order_id))["paid"] is False
    finally:
        _run(_teardown_org(org_id))


def test_unknown_payment_method_is_refused(client):
    org_id = _run(_seed_org("Pago Metodo Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Pago"))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="en_preparacion",
        ))

        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "bitcoin"}, headers=_auth(token),
        )
        assert resp.status_code == 400, resp.text
        assert _run(_fetch_order(order_id))["paid"] is False
    finally:
        _run(_teardown_org(org_id))


def test_assigned_courier_may_register_payment_but_another_courier_may_not(client):
    """The rider at the door is who holds the cash — but only for THEIR own
    order (app/routes/staff_delivery.py::_require_can_transition)."""
    org_id = _run(_seed_org("Pago Domiciliario Org"))
    try:
        location_id = _run(_seed_location(org_id, "Sede Pago"))
        mine_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        other_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        # A fresh pool between the two token creations: each _run() spins up
        # its own event loop, and sessions_repo's shared asyncpg pool cannot
        # be reused across them ("another operation is in progress").
        mine_token = _run(_create_staff_token(mine_id))
        _reset_pool()
        other_token = _run(_create_staff_token(other_id))
        _reset_pool()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id,
            status="en_camino", courier_staff_id=mine_id,
        ))

        refused = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "efectivo"}, headers=_auth(other_token),
        )
        assert refused.status_code == 403, refused.text
        assert _run(_fetch_order(order_id))["paid"] is False

        ok = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "efectivo"}, headers=_auth(mine_token),
        )
        assert ok.status_code == 200, ok.text
        assert _run(_fetch_order(order_id))["paid"] is True
    finally:
        _run(_teardown_org(org_id))


def test_cashier_cannot_register_payment_on_another_sede_order(client):
    org_id = _run(_seed_org("Pago Sede Org"))
    try:
        mine = _run(_seed_location(org_id, "Sede Mia"))
        other = _run(_seed_location(org_id, "Sede Ajena"))
        cashier_id = _run(_seed_staff(org_id, mine, role="caja"))
        token = _run(_create_staff_token(cashier_id))
        order_id = _run(_seed_order(
            org_id=org_id, location_id=other, status="en_preparacion",
        ))

        resp = _post(
            client, f"/api/staff/delivery/orders/{order_id}/mark-paid",
            json={"payment_method": "efectivo"}, headers=_auth(token),
        )
        assert resp.status_code == 404, resp.text
        assert _run(_fetch_order(order_id))["paid"] is False
    finally:
        _run(_teardown_org(org_id))
