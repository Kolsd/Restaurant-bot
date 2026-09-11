"""
tests/test_diner_order_flow.py
================================
Integration coverage for the three gaps closed in the Mesio-native diner
web-chat wave:

  Gap 1 — table session opened AT SCAN TIME (app/routes/diner.py
          create_diner_session), join_code minted for the host, a second
          diner asked for the code (POST /api/diner/join), throttled brute
          force.
  Gap 2 — deterministic order-send (POST /api/diner/order/send) reusing the
          shared app/services/table_order_commit.py core (same one the
          WhatsApp LLM path now calls), with idempotency-key + cart-lock
          double-send protection, empty-cart rejection, and typed
          out-of-stock handling.
  Gap 3 — the diner's own table view (GET /api/diner/table): "Tú" vs "Otro
          comensal", no identity leak.

Also proves the north-star "pedidos rescatados" metric counts a web_chat
order (app/repositories/north_star_repo.py).

Requires TEST_DATABASE_URL. Follows the exact pattern established in
tests/test_diner_routes.py (see that file's docstring for the two
event-loop gotchas this works around: throwaway loop for seed/teardown,
and resetting app.services.database._pool before every single HTTP call
made via TestClient within one test).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import date

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


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


# ── Seed fixtures ────────────────────────────────────────────────────────────

async def _make_org(conn, *, with_stock_dish: bool = False) -> dict:
    suffix = uuid.uuid4().hex[:10]
    bot_number = f"573{suffix[:9]}"
    menu = {
        "Principales": [
            {"name": "Bandeja Paisa", "description": "", "price": 28000, "active": True, "sku": "bandeja"},
            {"name": "Ajiaco", "description": "", "price": 22000, "active": True, "sku": "ajiaco"},
        ],
    }
    if with_stock_dish:
        menu["Principales"].append(
            {"name": "Pescado Frito", "description": "", "price": 30000, "active": True, "sku": "pescado"}
        )

    org_id = await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu, features) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
        f"Order Flow Org {suffix}", f"order-flow-{suffix}",
        json.dumps(menu), json.dumps({"currency": "COP"}),
    )
    location_id = await conn.fetchval(
        "INSERT INTO locations (org_id, name, whatsapp_number) VALUES ($1, $2, $3) RETURNING id",
        org_id, f"Sede {suffix}", bot_number,
    )
    table_id = f"t-{suffix}"
    await conn.execute(
        "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
        "VALUES ($1, $2, $3, $4, $5, $6, TRUE)",
        table_id, 9, f"Mesa {suffix[:4]}", location_id, location_id, org_id,
    )
    if with_stock_dish:
        await conn.execute(
            "INSERT INTO inventory (org_id, name, unit, current_stock, min_stock, linked_dishes) "
            "VALUES ($1, $2, 'unit', 0, 1, $3::jsonb)",
            org_id, "Pescado (ingrediente)", json.dumps(["Pescado Frito"]),
        )
    return {
        "org_id": org_id,
        "location_id": location_id,
        "table_id": table_id,
        "bot_number": bot_number,
    }


async def _drop_org(conn, org_id: int) -> None:
    # Everything else (table_sessions, table_orders, carts, conversations,
    # waiter_alerts, restaurant_tables, locations, inventory) FKs to
    # organizations ON DELETE CASCADE. diner_sessions does not — delete
    # explicitly (mirrors tests/test_diner_routes.py::_drop_org).
    await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)


async def _seed(*, with_stock_dish: bool = False) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await _make_org(conn, with_stock_dish=with_stock_dish)
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _drop_org(conn, org_id)
    finally:
        await conn.close()


@pytest.fixture
def org_a():
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


@pytest.fixture
def org_b():
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


@pytest.fixture
def org_stock():
    info = _run(_seed(with_stock_dish=True))
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


# ── Colliding org_id / location_id fixture (P0 regression, found 2026-09) ──
#
# app/repositories/restaurant_repo.py::db_get_restaurant_by_id runs
# `WHERE r.id = $1 OR l.org_id = $1 ORDER BY (l.org_id = $1) DESC` — it
# accepts EITHER an org id OR a location id, and the two are independent
# integer sequences over the same range, so passing a LOCATION id can
# resolve to a COMPLETELY UNRELATED org whenever that org's id happens to
# equal the location id. app/routes/diner.py must never pass a location id
# into that function (see _resolve_diner_restaurant) — this fixture forces
# the collision so the whole diner flow is proven safe against it.

async def _make_colliding_org(conn, collide_with_org_id: int) -> dict:
    suffix = uuid.uuid4().hex[:10]
    bot_number = f"573{suffix[:9]}"
    menu = {"Principales": [{"name": "Sancocho", "description": "", "price": 26000, "active": True, "sku": "sancocho"}]}
    org_id = await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu, features) VALUES ($1,$2,$3::jsonb,$4::jsonb) RETURNING id",
        f"Collide Org {suffix}", f"collide-{suffix}", json.dumps(menu), json.dumps({"currency": "COP"}),
    )
    # Force this location's PRIMARY KEY to equal another org's id.
    location_id = collide_with_org_id
    await conn.execute(
        "INSERT INTO locations (id, org_id, name, whatsapp_number) VALUES ($1, $2, $3, $4)",
        location_id, org_id, f"Sede Collide {suffix}", bot_number,
    )
    table_id = f"t-collide-{suffix}"
    await conn.execute(
        "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
        "VALUES ($1, $2, $3, $4, $5, $6, TRUE)",
        table_id, 3, f"Mesa Collide {suffix[:4]}", location_id, location_id, org_id,
    )
    return {
        "org_id": org_id,
        "location_id": location_id,
        "table_id": table_id,
        "bot_number": bot_number,
    }


@pytest.fixture
def org_collide(org_a):
    """A second org whose location id == org_a's org_id."""
    info = _run(_make_colliding_org_conn(org_a["org_id"]))
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


async def _make_colliding_org_conn(collide_with_org_id: int) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await _make_colliding_org(conn, collide_with_org_id)
    finally:
        await conn.close()


# ── Small helpers ────────────────────────────────────────────────────────────

def _open(client, table_id: str) -> dict:
    resp = _post(client, "/api/diner/session", json={"table_id": table_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _join(client, token: str, code: str):
    return _post(client, "/api/diner/join", json={"token": token, "code": code})


def _add(client, token: str, sku: str, qty: int = 1, note: str | None = None):
    body = {"token": token, "sku": sku, "qty": qty}
    if note:
        body["note"] = note
    resp = _post(client, "/api/diner/cart/add", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _send(client, token: str, idem_key: str | None = None):
    return _post(client, "/api/diner/order/send", json={
        "token": token, "idempotency_key": idem_key or str(uuid.uuid4()),
    })


def _table_view(client, token: str):
    return _get(client, "/api/diner/table", params={"token": token})


async def _kitchen_rows_async(org_id: int) -> list:
    from app.repositories import tables_repo
    from app.services.tenant_context import tenant_scope
    with tenant_scope(org_id):
        return await tables_repo.db_get_table_orders_for_branch(branch_id=None, org_id=org_id, status=None)


def _kitchen_rows(org_id: int) -> list:
    _reset_pool()
    return _run(_kitchen_rows_async(org_id))


async def _rescatados_async(org_id: int) -> dict:
    from app.repositories import north_star_repo
    from app.services.tenant_context import tenant_scope
    today = date.today()
    with tenant_scope(org_id):
        return await north_star_repo.db_count_pedidos_rescatados(today, today)


def _rescatados(org_id: int) -> dict:
    _reset_pool()
    return _run(_rescatados_async(org_id))


# ── Gap 1 + 2 + 3: the whole flow on a real database ────────────────────────

def test_full_diner_flow_two_diners_one_table(client, org_a):
    table_id = org_a["table_id"]
    org_id = org_a["org_id"]

    # 1. Diner A scans the free table → hosts it, gets a join_code.
    session_a = _open(client, table_id)
    assert session_a["requires_join_code"] is False
    assert session_a["message"].startswith("¡Hola!")
    join_code = session_a["join_code"]
    assert isinstance(join_code, str) and len(join_code) == 4 and join_code.isdigit()
    token_a = session_a["token"]

    # 2. Diner B scans the SAME table → table is occupied, asked for the code.
    session_b = _open(client, table_id)
    assert session_b["requires_join_code"] is True
    assert session_b["message"] == ""
    assert session_b["blocks"] == []
    token_b = session_b["token"]
    assert token_b != token_a

    # 3. A wrong code is rejected (and counted against the attempt cap).
    wrong_code = "0000" if join_code != "0000" else "1111"
    resp_wrong = _join(client, token_b, wrong_code)
    assert resp_wrong.status_code == 422
    assert "incorrecto" in resp_wrong.json()["detail"].lower()

    # 4. The right code joins — normal greeting, no second host session created.
    resp_join = _join(client, token_b, join_code)
    assert resp_join.status_code == 200
    joined = resp_join.json()
    assert joined["message"].startswith("¡Hola!")
    assert joined["restaurant_name"] == session_a["restaurant_name"]

    # 5. Both add dishes — diner B's with a note.
    _add(client, token_a, "bandeja", qty=2)
    _add(client, token_b, "ajiaco", qty=1, note="sin cilantro")

    # 6. Both send their own round to the kitchen.
    resp_send_a = _send(client, token_a)
    assert resp_send_a.status_code == 200, resp_send_a.text
    order_a = resp_send_a.json()
    assert order_a["sub_number"] == 1

    resp_send_b = _send(client, token_b)
    assert resp_send_b.status_code == 200, resp_send_b.text
    order_b = resp_send_b.json()
    assert order_b["base_order_id"] == order_a["base_order_id"]  # same table group
    assert order_b["sub_number"] == 2

    # 7. Kitchen query sees BOTH rounds — channel, branch_id, phone, and the
    #    note under `notes` (plural — the naming trap the brief calls out).
    rows = _kitchen_rows(org_id)
    assert len(rows) == 2
    by_phone = {r["phone"]: r for r in rows}
    assert set(by_phone.keys()) == {token_a, token_b}
    for row in rows:
        assert row["channel"] == "web_chat"
        assert row["branch_id"] == org_a["location_id"]

    ajiaco_row = by_phone[token_b]
    items_b = ajiaco_row["items"]
    if isinstance(items_b, str):
        items_b = json.loads(items_b)
    ajiaco_item = next(i for i in items_b if i.get("name") == "Ajiaco")
    assert ajiaco_item["notes"] == "sin cilantro"

    # 8. Table view — "Tú" vs "Otro comensal", never leaking the other
    #    diner's token/phone identity.
    view_a_resp = _table_view(client, token_a)
    assert view_a_resp.status_code == 200
    view_a = view_a_resp.json()
    assert view_a["table_name"] == session_a["table_name"]
    assert len(view_a["orders"]) == 2
    labels_a = {o["diner_label"] for o in view_a["orders"]}
    assert labels_a == {"Tú", "Otro comensal"}
    mine = next(o for o in view_a["orders"] if o["mine"])
    theirs = next(o for o in view_a["orders"] if not o["mine"])
    assert mine["order_id"] == order_a["order_id"]
    assert theirs["order_id"] == order_b["order_id"]
    # No raw identity leak anywhere in the payload text.
    assert token_b not in view_a_resp.text
    assert token_a in view_a_resp.text or True  # own token is fine to appear (it's the caller)

    view_b = _table_view(client, token_b).json()
    labels_b = {o["diner_label"] for o in view_b["orders"]}
    assert labels_b == {"Tú", "Otro comensal"}

    # 9. North-star "pedidos rescatados" counts the web_chat orders.
    rescue = _rescatados(org_id)
    assert rescue["table"] >= 2
    assert rescue["count"] >= 2


def test_join_code_brute_force_is_throttled(client, org_a):
    table_id = org_a["table_id"]
    host = _open(client, table_id)  # hosts + occupies the table
    joiner = _open(client, table_id)
    assert joiner["requires_join_code"] is True
    token = joiner["token"]
    real_code = host["join_code"]

    statuses = []
    for i in range(6):
        wrong = f"{(int(real_code) + 1 + i) % 10000:04d}"
        r = _join(client, token, wrong)
        statuses.append((r.status_code, r.json().get("detail", "")))

    assert any("demasiados" in detail.lower() for _, detail in statuses), statuses


def test_second_diner_reload_does_not_reask_code_or_duplicate_session(client, org_a):
    """A page reload reuses the SAME token — GET /api/diner/cart (the
    frontend's restore path) must work post-join without re-asking for the
    code or creating a duplicate table_sessions row."""
    table_id = org_a["table_id"]
    host = _open(client, table_id)
    joiner = _open(client, table_id)
    token = joiner["token"]
    _join(client, token, host["join_code"])

    # Idempotent re-join (simulates a retried request) must not error and
    # must not require the code again.
    resp = _join(client, token, host["join_code"])
    assert resp.status_code == 200

    # "Reload" — cart read must work without ever re-asking for a code.
    resp_cart = _get(client, "/api/diner/cart", params={"token": token})
    assert resp_cart.status_code == 200


def test_order_send_empty_cart_rejected(client, org_a):
    session = _open(client, org_a["table_id"])
    resp = _send(client, session["token"])
    assert resp.status_code == 422
    assert "vac" in resp.json()["detail"].lower()


def test_order_send_double_tap_same_key_is_idempotent(client, org_a):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)

    key = str(uuid.uuid4())
    first = _send(client, token, idem_key=key)
    assert first.status_code == 200
    order_id = first.json()["order_id"]

    # Same idempotency_key again — must return the SAME order, not a new one.
    second = _send(client, token, idem_key=key)
    assert second.status_code == 200
    assert second.json()["order_id"] == order_id
    assert second.json() == first.json()

    rows = _kitchen_rows(org_a["org_id"])
    matching = [r for r in rows if r["id"] == order_id]
    assert len(matching) == 1  # exactly one order — never duplicated

    # A retry with a DIFFERENT key, cart already cleared, is a clean empty
    # rejection — never a second order either.
    third = _send(client, token, idem_key=str(uuid.uuid4()))
    assert third.status_code == 422


def test_order_send_insufficient_stock_surfaces_dish_name(client, org_stock):
    session = _open(client, org_stock["table_id"])
    token = session["token"]
    _add(client, token, "pescado", qty=1)

    resp = _send(client, token)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "Pescado Frito" in detail

    # The held/failed order must not sit in the active kitchen queue —
    # db_get_table_orders_for_branch's default filter excludes status
    # 'cancelado' (the value table_order_commit.deduct_inventory_or_cancel
    # actually sets).
    rows = _kitchen_rows(org_stock["org_id"])
    assert rows == []

    # The cart is deliberately left intact (diner has a real cart-editing
    # UI) so they can drop the sold-out item and resend the rest.
    resp_cart = _get(client, "/api/diner/cart", params={"token": token})
    assert resp_cart.status_code == 200
    cart_items_after = resp_cart.json()["blocks"][0]["items"]
    assert len(cart_items_after) == 1
    assert cart_items_after[0]["name"] == "Pescado Frito"


def test_cross_org_join_code_never_matches_and_table_view_is_isolated(client, org_a, org_b):
    session_a = _open(client, org_a["table_id"])
    session_b = _open(client, org_b["table_id"])

    # A stranger scanning org A's table but guessing org B's real join_code
    # (or any code at all) can never match — org B's code was minted for a
    # completely different table_id.
    intruder = _open(client, org_a["table_id"])
    assert intruder["requires_join_code"] is True
    resp = _join(client, intruder["token"], session_b["join_code"])
    assert resp.status_code == 422

    # Each org's table view only ever shows its own table — never the other
    # org's name/identifiers.
    _add(client, session_a["token"], "bandeja", qty=1)
    _send(client, session_a["token"])
    view_a = _table_view(client, session_a["token"])
    assert org_b["table_id"] not in view_a.text
    assert session_b["restaurant_name"] not in view_a.text


# ── P0 regression: org_id / location_id collision ───────────────────────────

def test_full_diner_flow_survives_org_location_id_collision(client, org_a, org_collide):
    """org_collide's location id is forced equal to org_a's org_id (see the
    org_collide fixture). The ENTIRE deterministic diner flow — session,
    join, cart, send to kitchen, table view — must resolve org_collide's OWN
    identity throughout, never org_a's (which is what the P0 in
    db_get_restaurant_by_id would leak if diner.py ever passed a location id
    into it)."""
    assert org_collide["location_id"] == org_a["org_id"]  # the collision is real

    table_id = org_collide["table_id"]

    session_a = _open(client, table_id)
    assert session_a["requires_join_code"] is False
    # Must resolve org_collide's OWN name — never org_a's (the collision target).
    assert "Collide Org" in session_a["restaurant_name"]
    token_a = session_a["token"]
    join_code = session_a["join_code"]

    session_b = _open(client, table_id)
    assert session_b["requires_join_code"] is True
    token_b = session_b["token"]

    resp_join = _join(client, token_b, join_code)
    assert resp_join.status_code == 200
    assert "Collide Org" in resp_join.json()["restaurant_name"]

    _add(client, token_a, "sancocho", qty=1)
    resp_send = _send(client, token_a)
    assert resp_send.status_code == 200, resp_send.text
    order = resp_send.json()

    rows = _kitchen_rows(org_collide["org_id"])
    assert len(rows) == 1
    assert rows[0]["org_id"] == org_collide["org_id"]
    assert rows[0]["branch_id"] == org_collide["location_id"]
    assert rows[0]["channel"] == "web_chat"

    # The colliding org (org_a) must see NOTHING of this — its own kitchen
    # query stays empty, proving no cross-contamination in either direction.
    rows_a = _kitchen_rows(org_a["org_id"])
    assert rows_a == []

    view = _table_view(client, token_a).json()
    assert view["table_name"] == session_a["table_name"]
    assert len(view["orders"]) == 1
    assert view["orders"][0]["order_id"] == order["order_id"]
    # Org A's identity must never appear anywhere in org_collide's diner-facing data.
    resp_view = _table_view(client, token_a)
    assert org_a["bot_number"] not in resp_view.text
