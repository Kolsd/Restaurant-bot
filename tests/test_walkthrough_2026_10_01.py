"""
tests/test_walkthrough_2026_10_01.py
====================================
What the 2026-10-01 dashboard walk-through found broken, held by tests that
go through the real routes, a real signup and a real login (no faked auth):

  - Configuración rewrote the sede whose id equalled the ORG id — another
    customer's, when the numbers collided (locations has no RLS);
  - any logged-in employee could create an admin or promote themselves;
    saving the Team form renamed the employee's login;
  - the ticket endpoint read across tenants;
  - the Salón map said "Libre" mid-dinner and listed a table twice when two
    diners sat at it;
  - the order history never showed a table; the pause button paused nothing;
  - an evening reservation was refused as "in the past" (UTC vs Bogotá) and
    Pro customers were locked out of reservations by a legacy flag;
  - a sitting opened by the staff left its guests asking for a code that
    existed nowhere;
  - NPS answers at a table were stored without their sede.

Synchronous on purpose, like tests/test_self_serve_signup.py: one TestClient
for the module, and verification through short asyncio.run connections.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.main import app

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")

_PASSWORD = "unaClaveSegura1"


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("DISABLE_EMBEDDED_WORKER", "1")
    with TestClient(app) as c:
        yield c


def _run(coro):
    return asyncio.run(coro)


async def _q(sql: str, *args, fetch: str = "row", org_id: int | None = None):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if org_id is not None:
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
        if fetch == "row":
            row = await conn.fetchrow(sql, *args)
            return dict(row) if row else {}
        if fetch == "all":
            return [dict(r) for r in await conn.fetch(sql, *args)]
        if fetch == "val":
            return await conn.fetchval(sql, *args)
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _purge(orgs: list[int], usernames: list[str]):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if usernames:
            await conn.execute("DELETE FROM users WHERE username = ANY($1::text[])", usernames)
        for org_id in orgs:
            await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
            for table in ("diner_sessions", "staff", "reservations", "nps_responses", "subscription_usage"):
                await conn.execute(f"DELETE FROM {table} WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
            await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def made():
    orgs: list[int] = []
    usernames: list[str] = []
    yield orgs, usernames
    _run(_purge(orgs, usernames))


def _owner(client, made, plan: str = "Restaurante") -> dict:
    """A real self-serve restaurant: signup, then login with the typed password."""
    orgs, usernames = made
    unique = uuid.uuid4().hex[:10]
    body = {
        "nombre": "Dueño Prueba", "email": f"owner.{unique}@ejemplo.com",
        "telefono": "+57 300 000 0000", "restaurante": f"Prueba {unique}",
        "ciudad": "Bogotá", "plan": plan, "password": _PASSWORD,
    }
    data = client.post("/api/signup", json=body).json()
    orgs.append(data["org_id"])
    usernames.append(data["username"])
    login = client.post("/api/auth/login", json={"username": body["email"], "password": _PASSWORD})
    assert login.status_code == 200, login.text
    return {
        "org_id": data["org_id"], "location_id": data["location_id"],
        "headers": {"Authorization": "Bearer " + login.json()["token"]},
    }


def _table(org_id: int, location_id: int, name: str = "1") -> str:
    table_id = f"t-{uuid.uuid4().hex[:10]}"
    _run(_q(
        "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active, capacity) "
        "VALUES ($1, 1, $2, $3, $4, $5, TRUE, 4)",
        table_id, name, location_id, location_id, org_id, fetch="none", org_id=org_id,
    ))
    return table_id


# ── Configuración: never another tenant's sede ──────────────────────────────

def test_settings_never_rewrite_the_sede_that_shares_the_org_id(client, made):
    me = _owner(client, made)
    other = _owner(client, made)
    org_id = me["org_id"]
    # Make a sede whose id IS my org id, belonging to the other customer (if
    # that id is free), or use whichever sede already holds it.
    holder = _run(_q("SELECT org_id FROM locations WHERE id = $1", org_id))
    if not holder:
        _run(_q("INSERT INTO locations (id, org_id, name, address) VALUES ($1, $2, 'Ajena', 'Calle ajena 1')",
                org_id, other["org_id"], fetch="none"))
    before = _run(_q("SELECT org_id, name, address FROM locations WHERE id = $1", org_id))

    resp = client.post("/api/settings", headers=me["headers"], json={
        "name": "Marca Nueva", "address": "Cra 7 #45-10",
        "opening_hours": {"sunday": {"open": None, "close": None, "closed": True}},
        "payment_instructions": {"nequi": "Nequi 300 000 0000"},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["location_id"] == me["location_id"]
    assert data["address"] == "Cra 7 #45-10"
    assert data["opening_hours"]["sunday"]["closed"] is True
    assert data["payment_instructions"]["nequi"] == "Nequi 300 000 0000"

    if before["org_id"] != org_id:
        after = _run(_q("SELECT org_id, name, address FROM locations WHERE id = $1", org_id))
        assert after == before, "another customer's sede was rewritten"
    mine = _run(_q("SELECT address FROM locations WHERE id = $1", me["location_id"]))
    assert mine["address"] == "Cra 7 #45-10"
    org = _run(_q("SELECT name FROM organizations WHERE id = $1", org_id))
    assert org["name"] == "Marca Nueva"


def test_settings_with_several_sedes_needs_a_sede_for_address_and_hours(client, made):
    me = _owner(client, made)
    _run(_q("INSERT INTO locations (org_id, name) VALUES ($1, 'Segunda')", me["org_id"], fetch="none"))
    resp = client.post("/api/settings", headers=me["headers"], json={"address": "Calle 1"})
    assert resp.status_code == 422
    picked = client.post("/api/settings", headers={**me["headers"], "X-Branch-ID": str(me["location_id"])},
                         json={"address": "Calle 1"})
    assert picked.status_code == 200, picked.text
    assert picked.json()["address"] == "Calle 1"


# ── Team: who may manage the roster ─────────────────────────────────────────

def test_a_waiter_cannot_manage_the_team_and_edits_keep_the_login(client, made):
    me = _owner(client, made)
    created = client.post("/api/staff", headers=me["headers"],
                          json={"name": "Ana Mesera", "role": "mesero", "password": "4321"})
    assert created.status_code == 201, created.text
    staff = created.json()["staff"]
    assert staff["location_id"] == me["location_id"]
    username = staff["username"]

    pin = client.post("/api/staff/pin-login",
                      json={"restaurant_id": me["org_id"], "name": "Ana Mesera", "pin": "4321"})
    assert pin.status_code == 200, pin.text
    waiter = {"Authorization": "Bearer " + pin.json()["token"]}
    assert client.post("/api/staff", headers=waiter,
                       json={"name": "Intruso", "role": "admin", "password": "9999"}).status_code == 403
    assert client.put(f"/api/staff/{staff['id']}", headers=waiter, json={"role": "admin"}).status_code == 403
    assert client.delete(f"/api/staff/{staff['id']}", headers=waiter).status_code == 403

    # Saving the form with the same name must not rename the login.
    edit = client.put(f"/api/staff/{staff['id']}", headers=me["headers"],
                      json={"name": "Ana Mesera", "password": "8765"})
    assert edit.status_code == 200, edit.text
    assert edit.json()["staff"]["username"] == username
    again = client.post("/api/staff/pin-login",
                        json={"restaurant_id": me["org_id"], "name": username, "pin": "8765"})
    assert again.status_code == 200, again.text


def test_owner_cannot_hand_out_an_unknown_role(client, made):
    me = _owner(client, made)
    resp = client.post("/api/staff", headers=me["headers"],
                       json={"name": "X", "role": "owner", "password": "4321"})
    assert resp.status_code == 422


# ── Tickets and the Salón map ───────────────────────────────────────────────

def _round(org_id, location_id, table_id, base_id, sub, items, total, status="recibido"):
    oid = base_id if sub == 1 else f"{base_id}-{sub}"
    _run(_q(
        "INSERT INTO table_orders (id, table_id, table_name, phone, items, status, total, base_order_id, "
        "sub_number, station, branch_id, location_id, org_id, created_at) "
        "VALUES ($1, $2, '1', 'web:x', $3::jsonb, $4, $5, $6, $7, 'all', $8, $9, $10, NOW())",
        oid, table_id, json.dumps(items), status, total, base_id, sub, location_id, location_id, org_id,
        fetch="none", org_id=org_id,
    ))
    return oid


def test_ticket_is_scoped_to_the_callers_org(client, made):
    me = _owner(client, made)
    other = _owner(client, made)
    t_other = _table(other["org_id"], other["location_id"])
    base = f"MESA-{uuid.uuid4().hex[:6].upper()}"
    _round(other["org_id"], other["location_id"], t_other, base, 1, [{"name": "Ajiaco", "price": 20000, "quantity": 1}], 20000)
    assert client.get(f"/api/table-orders/{base}/ticket", headers=me["headers"]).status_code == 404
    mine = client.get(f"/api/table-orders/{base}/ticket", headers=other["headers"])
    assert mine.status_code == 200 and mine.json()["total"] == 20000


def test_floor_plan_shows_the_open_bill_once_per_table(client, made):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    t = _table(org, loc)
    for phone in ("web:a", "web:b"):  # two diners at one table
        _run(_q("INSERT INTO table_sessions (phone, table_id, table_name, status, org_id, location_id, started_at) "
                "VALUES ($1, $2, '1', 'active', $3, $4, NOW() - INTERVAL '30 minutes')",
                phone, t, org, loc, fetch="none", org_id=org))
    base = f"MESA-{uuid.uuid4().hex[:6].upper()}"
    _round(org, loc, t, base, 1, [{"name": "Hamburguesa", "price": 25000, "quantity": 2}], 50000)
    _round(org, loc, t, base, 2, [{"name": "Hamburguesa", "price": 25000, "quantity": 2}], 50000)
    _round(org, loc, t, base, 3, [{"name": "Gaseosa", "price": 5000, "quantity": 1}], 5000, status="cancelled")

    plan = client.get("/api/tables/floor-plan", headers=me["headers"]).json()
    rows = [r for r in plan if r["id"] == t]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "eating"
    assert row["current_total"] == 100000
    assert row["current_orders"] == [{"name": "Hamburguesa", "quantity": 4, "price": 25000.0}]
    assert row["current_base_order_id"] == base
    assert row["opened_at"]


# ── Pedidos › Histórico ─────────────────────────────────────────────────────

def test_order_history_has_tables_and_web_orders_with_utc_times(client, made):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    t = _table(org, loc)
    base = f"MESA-{uuid.uuid4().hex[:6].upper()}"
    _round(org, loc, t, base, 1, [{"name": "Hamburguesa", "price": 25000, "quantity": 1}], 25000)
    _run(_q(
        "INSERT INTO orders (id, org_id, location_id, phone, items, order_type, status, subtotal, total, "
        "public_code, customer_name, channel, created_at) "
        "VALUES (gen_random_uuid()::text, $1, $2, 'web:y', $3::jsonb, 'recoger', 'entregado', 8000, 8000, "
        "'ABC123', 'Cliente', 'web', NOW() - INTERVAL '2 minutes')",
        org, loc, json.dumps([{"name": "Limonada", "quantity": 1}]), fetch="none", org_id=org,
    ))
    data = client.get("/api/stats/order-history?days=1", headers=me["headers"]).json()["orders"]
    by_id = {o["id"]: o for o in data}
    assert by_id[base]["channel"] == "qr" and by_id[base]["who"] == "1" and by_id[base]["total"] == 25000
    assert by_id["ABC123"]["source"] == "pickup" and by_id["ABC123"]["channel"] == "domicilio"
    assert by_id["ABC123"]["created_at"].endswith("+00:00")


# ── Pause, reservations, staff-opened sittings ──────────────────────────────

def test_the_pause_button_really_closes_the_restaurant(client, made):
    me = _owner(client, made)
    t = _table(me["org_id"], me["location_id"])
    assert client.post("/api/settings/pause", headers=me["headers"], json={"paused": True}).status_code == 200
    closed = client.post("/api/diner/session", json={"table_id": t})
    assert closed.status_code == 403
    assert client.post("/api/settings/pause", headers=me["headers"], json={"paused": False}).status_code == 200
    assert client.post("/api/diner/session", json={"table_id": t}).status_code == 200


def test_pro_books_an_evening_table_in_restaurant_time(client, made):
    me = _owner(client, made)
    _run(_q("UPDATE organizations SET plan_code = 'pro', subscription_plan = 'pro' WHERE id = $1",
            me["org_id"], fetch="none"))
    soon = datetime.now(ZoneInfo("America/Bogota")) + timedelta(hours=1)
    resp = client.post("/api/reservations", headers=me["headers"], json={
        "customer_name": "Reserva", "party_size": 2,
        "date": soon.strftime("%Y-%m-%d"), "time": soon.strftime("%H:%M"),
    })
    assert resp.status_code == 201, resp.text
    past = datetime.now(ZoneInfo("America/Bogota")) - timedelta(hours=1)
    refused = client.post("/api/reservations", headers=me["headers"], json={
        "customer_name": "Tarde", "party_size": 2,
        "date": past.strftime("%Y-%m-%d"), "time": past.strftime("%H:%M"),
    })
    assert refused.status_code == 400
    assert "futuras" in refused.json()["detail"]


def test_guests_of_a_staff_opened_sitting_can_join_it(client, made):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    t = _table(org, loc)
    _run(_q("INSERT INTO table_sessions (phone, table_id, table_name, status, org_id, location_id, started_at) "
            "VALUES ('3000000001', $1, '1', 'active', $2, $3, NOW())", t, org, loc, fetch="none", org_id=org))
    first = client.post("/api/diner/session", json={"table_id": t}).json()
    assert first["requires_join_code"] is False
    assert first["join_code"]
    second = client.post("/api/diner/session", json={"table_id": t}).json()
    assert second["requires_join_code"] is True  # the code still protects the table


# ── NPS by sede, billing ────────────────────────────────────────────────────

def test_table_nps_is_stored_with_its_sede(client, made):
    me = _owner(client, made)
    org, loc = me["org_id"], me["location_id"]
    t = _table(org, loc)
    _run(_q("INSERT INTO table_sessions (phone, table_id, table_name, status, org_id, location_id, started_at) "
            "VALUES ('web:nps', $1, '1', 'closed', $2, $3, NOW())", t, org, loc, fetch="none", org_id=org))

    async def _save():
        from app.services import database as db_mod
        from app.repositories.conversations_repo import db_save_nps_response
        from app.services.tenant_context import tenant_scope
        db_mod._pool = None
        try:
            with tenant_scope(org):
                await db_save_nps_response("web:nps", org, 5, "")
        finally:
            pool = db_mod._pool
            db_mod._pool = None
            if pool is not None:
                await pool.close()
    _run(_save())
    row = _run(_q("SELECT location_id FROM nps_responses WHERE org_id = $1 AND phone = 'web:nps'", org, org_id=org))
    assert row["location_id"] == loc


def test_nps_formula_matches_the_nps_page():
    from app.repositories.stats_repo import _nps
    assert _nps(0, 0, 0) is None
    assert _nps(1, 0, 1) == 100
    assert _nps(2, 1, 4) == 25   # (2 - 1) / 4
    assert _nps(0, 3, 3) == -100


def test_accounting_setup_says_dian_is_pro(client, made):
    me = _owner(client, made)
    assert client.get("/api/billing/config", headers=me["headers"]).json()["dian_in_plan"] is False


# ── DIAN config: never another tenant's ─────────────────────────────────────

def test_billing_config_never_reads_or_writes_the_org_owning_sede_n(client, made):
    """get/save_billing_config also matched "the org that owns location #id";
    organizations has no RLS, so org N read and overwrote the DIAN
    credentials of whichever org owns sede N."""
    me = _owner(client, made, plan="Pro")
    other = _owner(client, made)
    org_id = me["org_id"]
    holder = _run(_q("SELECT org_id FROM locations WHERE id = $1", org_id))
    if not holder:
        _run(_q("INSERT INTO locations (id, org_id, name, address) VALUES ($1, $2, 'Ajena', 'Calle ajena 1')",
                org_id, other["org_id"], fetch="none"))
        holder = {"org_id": other["org_id"]}
    victim = int(holder["org_id"])
    if victim == org_id:
        pytest.fail("sede #org_id belongs to this org: the collision under test was not built")
    _run(_q("UPDATE organizations SET billing_config = $1::jsonb WHERE id = $2",
            json.dumps({"provider": "ajeno", "api_key": "secreto-ajeno"}), victim, fetch="none"))

    seen = client.get("/api/billing/config", headers=me["headers"]).json()
    assert seen["configured"] is False, "read another org's DIAN config"
    saved = client.post("/api/billing/config", headers=me["headers"], json={"provider": "alegra"})
    assert saved.status_code == 200, saved.text
    mine = client.get("/api/billing/config", headers=me["headers"]).json()
    assert mine["config"]["provider"] == "alegra"
    theirs = _run(_q("SELECT billing_config FROM organizations WHERE id = $1", victim))["billing_config"]
    theirs = theirs if isinstance(theirs, dict) else json.loads(theirs)
    assert theirs["provider"] == "ajeno", "overwrote another org's DIAN config"


def test_a_new_table_lands_on_the_owners_own_sede(client, made):
    """POST /api/tables fell back to the ORG id as a location id when no sede
    was picked: the table went to sede #org_id, another customer's."""
    me = _owner(client, made)
    resp = client.post("/api/tables", headers=me["headers"])
    assert resp.status_code == 200, resp.text
    row = _run(_q("SELECT location_id, branch_id, org_id FROM restaurant_tables WHERE id = $1",
                  resp.json()["table_id"], org_id=me["org_id"]))
    assert (row["location_id"], row["branch_id"], row["org_id"]) == (me["location_id"], me["location_id"], me["org_id"])

    # A header naming another customer's sede is ignored, never trusted.
    other = _owner(client, made)
    hdrs = dict(me["headers"], **{"X-Branch-ID": str(other["location_id"])})
    resp = client.post("/api/tables", headers=hdrs)
    assert resp.status_code == 422, resp.text


def test_an_order_is_readable_only_by_its_own_restaurant(client, made):
    """GET /orders/{id} compared a column orders no longer has, so the check
    never ran: any login read any restaurant's order, phone and address."""
    me = _owner(client, made)
    other = _owner(client, made)
    order_id = f"o-{uuid.uuid4().hex[:10]}"
    _run(_q(
        "INSERT INTO orders (id, phone, items, order_type, subtotal, total, org_id, location_id, address) "
        "VALUES ($1, '+573001112233', '[]'::jsonb, 'delivery', 10000, 10000, $2, $3, 'Calle privada 1')",
        order_id, me["org_id"], me["location_id"], fetch="none", org_id=me["org_id"],
    ))
    mine = client.get(f"/api/orders/{order_id}", headers=me["headers"])
    assert mine.status_code == 200, mine.text
    assert mine.json()["address"] == "Calle privada 1"
    theirs = client.get(f"/api/orders/{order_id}", headers=other["headers"])
    assert theirs.status_code == 404, theirs.text
