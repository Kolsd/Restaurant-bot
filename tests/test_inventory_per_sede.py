"""tests/test_inventory_per_sede.py

Stock belongs to ONE sede, and can be moved between sedes.

PM decision 2026-09-20: "el inventario es uno por sede, se puede hacer
intercambios de inventario por sede. Si el owner quiere anadir inventario
debera escoger la sede primero con un selector de sedes."

Before this, `inventory.location_id` existed but nothing ever wrote it: a
two-sede restaurant shared one stock number, every kitchen saw the same
fridge, and an order at sede B decremented whatever row the recipe pointed
at. Real DB throughout — what is asserted is which rows exist and what their
numbers are after a write.
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

_AUTH = {"Authorization": "Bearer test-token"}


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _run(coro):
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


def _delete(client, url, **kwargs):
    _reset_pool()
    return client.delete(url, **kwargs)


# -- Seeding ---------------------------------------------------------------

async def _seed() -> dict:
    suffix = uuid.uuid4().hex[:10]
    bot_number = f"573{suffix[:9]}"
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, menu, features, whatsapp_number) "
            "VALUES ($1,$2,$3::jsonb,$4::jsonb,$5) RETURNING id",
            f"Inv Sede {suffix}", f"inv-sede-{suffix}", "{}", "{}", bot_number,
        )
        loc_a = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1,'Sede A') RETURNING id", org_id)
        loc_b = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1,'Sede B') RETURNING id", org_id)

        items = {}
        for loc, label, stock in ((loc_a, "A", 10), (loc_b, "B", 3)):
            items[label] = await conn.fetchval(
                "INSERT INTO inventory (org_id, location_id, name, unit, current_stock, "
                "min_stock, linked_dishes, cost_per_unit) "
                "VALUES ($1,$2,$3,'kg',$4,1,$5::jsonb,0) RETURNING id",
                org_id, loc, "Tomate", stock, json.dumps([]),
            )
        # A product only sede A has — the transfer destination must be created.
        items["only_a"] = await conn.fetchval(
            "INSERT INTO inventory (org_id, location_id, name, unit, current_stock, "
            "min_stock, linked_dishes, cost_per_unit) "
            "VALUES ($1,$2,'Queso','kg',8,2,$3::jsonb,1500) RETURNING id",
            org_id, loc_a, json.dumps([]),
        )
        return {"org_id": org_id, "loc_a": loc_a, "loc_b": loc_b,
                "items": items, "bot_number": bot_number}
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            "DELETE FROM inventory_history WHERE inventory_id IN "
            "(SELECT id FROM inventory WHERE org_id = $1)", org_id)
        for table in ("inventory", "dish_recipes", "menu_availability", "users"):
            await conn.execute(f"DELETE FROM {table} WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


async def _stock_of(item_id: int) -> float:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        v = await conn.fetchval("SELECT current_stock FROM inventory WHERE id = $1", item_id)
        return float(v) if v is not None else None
    finally:
        await conn.close()


async def _find_item(org_id: int, location_id: int, name: str) -> dict | None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT * FROM inventory WHERE org_id=$1 AND location_id=$2 AND lower(name)=lower($3)",
            org_id, location_id, name)
        return dict(row) if row else None
    finally:
        await conn.close()


@pytest.fixture
def org():
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


def _auth_as(monkeypatch, *, org_id: int, location_id: int | None, role: str,
             username: str = "inv_user"):
    from app.services import database as db

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": org_id, "org_id": org_id,
            "location_id": location_id, "role": role, "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


# -- Reads are per sede ----------------------------------------------------

def test_kitchen_sees_only_its_own_sede_stock(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=org["loc_b"], role="cocina")

    resp = _get(client, "/api/inventory", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert {i["name"] for i in items} == {"Tomate"}, items
    # Sede B holds 3kg; sede A's 10kg and its Queso must not be visible.
    assert float(items[0]["current_stock"]) == 3.0
    assert resp.json()["location_id"] == org["loc_b"]


def test_owner_sees_every_sede_and_can_narrow_to_one(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    every = _get(client, "/api/inventory", headers=_AUTH)
    assert every.status_code == 200, every.text
    assert len(every.json()["items"]) == 3

    just_b = _get(client, "/api/inventory",
                  headers={**_AUTH, "X-Branch-ID": str(org["loc_b"])})
    assert just_b.status_code == 200, just_b.text
    names = [i["name"] for i in just_b.json()["items"]]
    assert names == ["Tomate"]


def test_a_cook_cannot_touch_another_sedes_item(client, org, monkeypatch):
    """Editing, deleting and adjusting all went through an org_id-only check."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=org["loc_b"], role="cocina")
    foreign = org["items"]["A"]

    upd = client.put(f"/api/inventory/{foreign}", headers=_AUTH, json={"current_stock": 999})
    assert upd.status_code == 404, upd.text

    adj = _post(client, f"/api/inventory/{foreign}/adjust", headers=_AUTH,
                json={"quantity": 50, "reason": "compra"})
    assert adj.status_code == 404, adj.text

    dele = _delete(client, f"/api/inventory/{foreign}", headers=_AUTH)
    assert dele.status_code == 404, dele.text

    assert _run(_stock_of(foreign)) == 10.0, "the other sede's stock must be untouched"


# -- Creating requires picking a sede --------------------------------------

def test_owner_must_pick_a_sede_before_adding_stock(client, org, monkeypatch):
    """PM: the owner chooses the sede with a selector first. With the sidebar
    on "todas las sedes" and no location in the body, this is a 400 — not a
    silent write into a shared pool that no longer exists."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, "/api/inventory", headers=_AUTH, json={
        "name": "Cebolla", "unit": "kg", "current_stock": 5, "min_stock": 1,
    })
    assert resp.status_code == 400, resp.text
    assert "sede" in resp.json()["detail"].lower()


def test_owner_creates_into_the_sede_they_picked(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, "/api/inventory", headers=_AUTH, json={
        "name": "Cebolla", "unit": "kg", "current_stock": 5, "min_stock": 1,
        "location_id": org["loc_b"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["location_id"] == org["loc_b"]
    assert _run(_find_item(org["org_id"], org["loc_b"], "Cebolla")) is not None
    assert _run(_find_item(org["org_id"], org["loc_a"], "Cebolla")) is None


def test_a_cooks_new_item_lands_in_their_own_sede_whatever_they_send(client, org, monkeypatch):
    """A body naming another sede is ignored, not trusted."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=org["loc_b"], role="cocina")

    resp = _post(client, "/api/inventory", headers=_AUTH, json={
        "name": "Ajo", "unit": "kg", "current_stock": 2, "min_stock": 0,
        "location_id": org["loc_a"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["item"]["location_id"] == org["loc_b"]


def test_owner_cannot_create_into_another_orgs_sede(client, org, monkeypatch):
    other = _run(_seed())
    try:
        _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
                 role="owner", username="inv_owner")
        resp = _post(client, "/api/inventory", headers=_AUTH, json={
            "name": "Fuga", "unit": "kg", "current_stock": 1, "min_stock": 0,
            "location_id": other["loc_a"],
        })
        assert resp.status_code == 404, resp.text
    finally:
        _run(_teardown(other["org_id"]))


# -- Transfers between sedes -----------------------------------------------

def test_transfer_moves_stock_between_existing_rows(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, f"/api/inventory/{org['items']['A']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_b"], "quantity": 4, "note": "falta en B"})
    assert resp.status_code == 200, resp.text

    assert _run(_stock_of(org["items"]["A"])) == 6.0
    assert _run(_stock_of(org["items"]["B"])) == 7.0


def test_transfer_creates_the_product_at_a_sede_that_never_had_it(client, org, monkeypatch):
    """Receiving something you do not stock yet is the normal case."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")
    assert _run(_find_item(org["org_id"], org["loc_b"], "Queso")) is None

    resp = _post(client, f"/api/inventory/{org['items']['only_a']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_b"], "quantity": 3})
    assert resp.status_code == 200, resp.text

    created = _run(_find_item(org["org_id"], org["loc_b"], "Queso"))
    assert created is not None
    assert float(created["current_stock"]) == 3.0
    # unit / min_stock / cost carried over from the source
    assert created["unit"] == "kg"
    assert float(created["min_stock"]) == 2.0
    assert float(created["cost_per_unit"]) == 1500.0
    assert _run(_stock_of(org["items"]["only_a"])) == 5.0


def test_transfer_over_available_stock_is_refused_and_writes_nothing(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, f"/api/inventory/{org['items']['B']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_a"], "quantity": 99})
    assert resp.status_code == 409, resp.text

    assert _run(_stock_of(org["items"]["B"])) == 3.0
    assert _run(_stock_of(org["items"]["A"])) == 10.0


def test_transfer_to_the_same_sede_is_refused(client, org, monkeypatch):
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, f"/api/inventory/{org['items']['A']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_a"], "quantity": 1})
    assert resp.status_code == 400, resp.text


def test_transfer_to_another_orgs_sede_is_refused(client, org, monkeypatch):
    other = _run(_seed())
    try:
        _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
                 role="owner", username="inv_owner")
        resp = _post(client, f"/api/inventory/{org['items']['A']}/transfer", headers=_AUTH,
                     json={"to_location_id": other["loc_a"], "quantity": 1})
        assert resp.status_code == 400, resp.text
        assert _run(_stock_of(org["items"]["A"])) == 10.0
    finally:
        _run(_teardown(other["org_id"]))


def test_gerente_may_send_from_their_sede_but_not_from_another(client, org, monkeypatch):
    """The source must be a sede the caller may act on; receiving is not a
    privilege, so any sede of the org can be the destination."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=org["loc_b"],
             role="gerente", username="inv_gerente")

    # Pulling stock OUT of sede A, which is not theirs.
    pull = _post(client, f"/api/inventory/{org['items']['A']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_b"], "quantity": 2})
    assert pull.status_code == 404, pull.text
    assert _run(_stock_of(org["items"]["A"])) == 10.0

    # Sending out of their own sede is fine.
    push = _post(client, f"/api/inventory/{org['items']['B']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_a"], "quantity": 2})
    assert push.status_code == 200, push.text
    assert _run(_stock_of(org["items"]["B"])) == 1.0
    assert _run(_stock_of(org["items"]["A"])) == 12.0


def test_transfer_records_both_sides_in_the_movement_history(client, org, monkeypatch):
    """Each sede's log has to explain where the stock went or came from."""
    _auth_as(monkeypatch, org_id=org["org_id"], location_id=None,
             role="owner", username="inv_owner")

    resp = _post(client, f"/api/inventory/{org['items']['A']}/transfer", headers=_AUTH,
                 json={"to_location_id": org["loc_b"], "quantity": 4})
    assert resp.status_code == 200, resp.text

    async def _history(item_id):
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            rows = await conn.fetch(
                "SELECT quantity_delta, reason FROM inventory_history "
                "WHERE inventory_id = $1 ORDER BY id DESC", item_id)
            return [dict(r) for r in rows]
        finally:
            await conn.close()

    out = _run(_history(org["items"]["A"]))
    assert out and float(out[0]["quantity_delta"]) == -4.0
    assert out[0]["reason"].startswith("traslado_salida:")

    incoming = _run(_history(org["items"]["B"]))
    assert incoming and float(incoming[0]["quantity_delta"]) == 4.0
    assert incoming[0]["reason"].startswith("traslado_entrada:")


# -- Consumption comes out of the right fridge -----------------------------

def test_an_order_deducts_from_its_own_sede(client, org):
    """A recipe is org-level and names ONE ingredient row, but the sede that
    cooks is the one whose stock must drop. Before this, an order at sede B
    decremented whatever row the recipe happened to point at — sede A's."""
    from app.repositories.orders_repo import deduct_inventory_in_tx
    from app.services.tenant_context import tenant_scope
    from app.services.tenant_db import tenant_connection

    async def _add_recipe():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            # The recipe points at SEDE A's Tomate row.
            await conn.execute(
                "INSERT INTO dish_recipes (org_id, dish_name, ingredient_id, quantity) "
                "VALUES ($1,'Ensalada',$2,2)", org["org_id"], org["items"]["A"])
        finally:
            await conn.close()

    _run(_add_recipe())

    async def _cook_at_b():
        with tenant_scope(org["org_id"]):
            async with tenant_connection() as conn:
                await deduct_inventory_in_tx(
                    conn, org["org_id"], [{"name": "Ensalada", "quantity": 1}],
                    location_id=org["loc_b"],
                )

    _run(_cook_at_b())

    assert _run(_stock_of(org["items"]["B"])) == 1.0, "sede B cooked, sede B pays"
    assert _run(_stock_of(org["items"]["A"])) == 10.0, "sede A must not be touched"
