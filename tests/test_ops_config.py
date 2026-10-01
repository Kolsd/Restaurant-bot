"""
tests/test_ops_config.py
========================
"Configurar operación" (migration 0105, app/services/ops_config.py).

Before it, every order showed on BOTH the kitchen and the bar screen: the bar
split existed in code but nothing could configure it. Now each sede says
which screens it uses and which carta categories go to the bar; the staff
sidebar and the kitchen/bar routing follow that answer.

Auth is the only thing faked here (get_current_user returns a fixed user);
the config, the sections and the orders go through the real routes and DB.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from app.services import ops_config
from app.services.table_order_commit import resolve_station_split

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")

ALL = ["cashier", "delivery", "waiter", "kitchen", "bar", "courier"]


# ── Unit ─────────────────────────────────────────────────────────────────────

def test_an_unconfigured_sede_keeps_every_screen():
    cfg = ops_config.normalize({})
    assert cfg["configured"] is False
    assert ops_config.visible_sections(ALL, cfg) == ALL
    # ...and the org's legacy features still decide the split.
    assert ops_config.station_features({"bar_enabled": True, "bar_categories": ["X"]}, cfg)["bar_enabled"] is True


def test_a_configured_sede_drops_what_it_does_not_use():
    cfg = ops_config.normalize({"configured": True, "bar": False, "delivery": True,
                                "courier": False, "waiter": True, "bar_categories": ["Bebidas"]})
    assert cfg["bar_categories"] == []  # no bar, no bar categories
    assert ops_config.visible_sections(ALL, cfg) == ["cashier", "delivery", "waiter", "kitchen"]
    assert ops_config.station_features({"bar_enabled": True, "bar_categories": ["Bebidas"]}, cfg)["bar_enabled"] is False


BAR = {"bar_enabled": True, "bar_categories": ["Bebidas"]}
FOOD = {"name": "Bandeja", "category": "Principales"}
DRINK = {"name": "Limonada", "category": "Bebidas"}


@pytest.mark.parametrize("items,features,expected", [
    ([FOOD, DRINK], BAR, ("kitchen", True)),
    ([FOOD], BAR, ("kitchen", False)),
    ([DRINK], BAR, ("bar", False)),       # was "all": beers showed in the kitchen too
    ([FOOD, DRINK], {}, ("all", False)),  # no bar: one ticket every screen sees
])
def test_station_of_a_round(items, features, expected):
    split = resolve_station_split(items, features)
    assert (split["kitchen_station"], split["has_split"]) == expected


# ── Integration ──────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


async def _scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))


async def _seed() -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        menu = {
            "Principales": [{"name": "Bandeja Paisa", "price": 28000, "active": True, "sku": "bandeja"}],
            "Bebidas": [{"name": "Limonada", "price": 8000, "active": True, "sku": "limonada"}],
        }
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, menu, features) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
            f"Ops Org {suffix}", f"ops-{suffix}", json.dumps(menu), json.dumps({"currency": "COP"}),
        )
        await _scope(conn, org_id)
        locs = []
        for n in (1, 2):
            locs.append(await conn.fetchval(
                "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id", org_id, f"Sede {n} {suffix}",
            ))
        table_id = f"t-{suffix}"
        await conn.execute(
            "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
            "VALUES ($1, 1, '1', $2, $3, $4, TRUE)",
            table_id, locs[0], locs[0], org_id,
        )
        return {"org_id": org_id, "locs": locs, "table_id": table_id}
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def org():
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


@pytest.fixture
def as_user(monkeypatch):
    """Fake only the login: who is calling."""
    import app.routes.auth_routes as auth_routes
    import app.routes.staff_ops as staff_ops

    def _set(user: dict):
        async def _fake(request):
            return user
        monkeypatch.setattr(auth_routes, "get_current_user", _fake)
        monkeypatch.setattr(staff_ops, "get_current_user", _fake)
    return _set


def _call(client, method, url, sede=None, **kw):
    _reset_pool()
    headers = {"Authorization": "Bearer t"}
    if sede:
        headers["X-Branch-ID"] = str(sede)
    return client.request(method, url, headers=headers, **kw)


def _owner(org):
    return {"org_id": org["org_id"], "location_id": None, "role": "owner"}


GOOD = {"bar": True, "bar_categories": ["Bebidas"], "delivery": False, "courier": True, "waiter": True}


@needs_db
def test_owner_is_asked_once_and_the_sidebar_follows_the_answer(client, org, as_user):
    as_user(_owner(org))
    sede = org["locs"][0]
    before = _call(client, "GET", "/api/staff/sections", sede=sede).json()
    assert before["sections"] == ALL
    assert before["needs_setup"] is True and before["can_configure"] is True

    got = _call(client, "GET", "/api/staff/ops-config", sede=sede).json()
    assert got["location_id"] == sede
    assert sorted(got["categories"]) == ["Bebidas", "Principales"]  # jsonb keeps no key order
    assert got["config"]["configured"] is False

    saved = _call(client, "PUT", "/api/staff/ops-config", sede=sede, json=GOOD)
    assert saved.status_code == 200, saved.text
    # No Domicilios, so no Mis entregas either, whatever was sent.
    assert saved.json()["config"] == {
        "configured": True, "bar": True, "bar_categories": ["Bebidas"],
        "delivery": False, "courier": False, "waiter": True,
    }

    after = _call(client, "GET", "/api/staff/sections", sede=sede).json()
    assert after["sections"] == ["cashier", "waiter", "kitchen", "bar"]
    assert after["needs_setup"] is False

    # The other sede is its own restaurant: still unconfigured.
    other = _call(client, "GET", "/api/staff/sections", sede=org["locs"][1]).json()
    assert other["sections"] == ALL and other["needs_setup"] is True


@needs_db
def test_a_bar_needs_real_categories_of_the_carta(client, org, as_user):
    as_user(_owner(org))
    sede = org["locs"][0]
    for cats in ([], ["Inventada"]):
        resp = _call(client, "PUT", "/api/staff/ops-config", sede=sede, json={**GOOD, "bar_categories": cats})
        assert resp.status_code == 422, resp.text
    assert _call(client, "GET", "/api/staff/ops-config", sede=sede).json()["config"]["configured"] is False


@needs_db
def test_only_admins_configure_and_a_gerente_only_their_own_sede(client, org, as_user):
    as_user({"org_id": org["org_id"], "location_id": org["locs"][0], "role": "cocina"})
    assert _call(client, "GET", "/api/staff/ops-config").status_code == 403
    sections = _call(client, "GET", "/api/staff/sections").json()
    assert sections["sections"] == ["kitchen"] and sections["needs_setup"] is False

    as_user({"org_id": org["org_id"], "location_id": org["locs"][0], "role": "gerente"})
    # The header naming another sede is ignored for a gerente.
    got = _call(client, "GET", "/api/staff/ops-config", sede=org["locs"][1]).json()
    assert got["location_id"] == org["locs"][0]


@needs_db
def test_a_multi_sede_owner_must_pick_a_sede_first(client, org, as_user):
    as_user(_owner(org))
    assert _call(client, "GET", "/api/staff/ops-config").status_code == 422
    sections = _call(client, "GET", "/api/staff/sections").json()
    assert sections["needs_setup"] is False and sections["can_configure"] is False


@needs_db
def test_esencial_has_no_delivery_screens(client, org, as_user):
    async def _esencial():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute(
                "UPDATE organizations SET plan_code = 'esencial', subscription_plan = 'esencial', "
                "comp_until = NULL WHERE id = $1", org["org_id"],
            )
        finally:
            await conn.close()
    _run(_esencial())
    as_user(_owner(org))
    data = _call(client, "GET", "/api/staff/sections", sede=org["locs"][0]).json()
    assert "delivery" not in data["sections"] and "courier" not in data["sections"]
    assert _call(client, "GET", "/api/staff/ops-config", sede=org["locs"][0]).json()["delivery_in_plan"] is False


async def _stations(org_id: int) -> list[tuple]:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        rows = await conn.fetch(
            "SELECT station, items FROM table_orders WHERE org_id = $1 ORDER BY sub_number", org_id,
        )
        return [(r["station"], [i["name"] for i in (json.loads(r["items"]) if isinstance(r["items"], str) else r["items"])])
                for r in rows]
    finally:
        await conn.close()


@needs_db
def test_a_diner_round_splits_into_kitchen_and_bar_once_configured(client, org, as_user):
    as_user(_owner(org))
    assert _call(client, "PUT", "/api/staff/ops-config", sede=org["locs"][0], json=GOOD).status_code == 200

    _reset_pool()
    s = client.post("/api/diner/session", json={"table_id": org["table_id"]}).json()
    for sku in ("bandeja", "limonada"):
        _reset_pool()
        assert client.post("/api/diner/cart/add", json={"token": s["token"], "sku": sku, "qty": 1}).status_code == 200
    _reset_pool()
    assert client.post("/api/diner/order/send", json={"token": s["token"], "idempotency_key": "k1"}).status_code == 200

    # A second round of drinks only goes to the bar alone.
    _reset_pool()
    client.post("/api/diner/cart/add", json={"token": s["token"], "sku": "limonada", "qty": 2})
    _reset_pool()
    assert client.post("/api/diner/order/send", json={"token": s["token"], "idempotency_key": "k2"}).status_code == 200

    assert _run(_stations(org["org_id"])) == [
        ("kitchen", ["Bandeja Paisa"]),
        ("bar", ["Limonada"]),
        ("bar", ["Limonada"]),
    ]
