"""The carta per sede (migration 0093): the organization's menu plus each
sede's changes — its own price, a hidden dish, dishes of its own.

PM 2026-09-20/21: each sede is its own restaurant; the carta is the org's
with per-sede overrides; a gerente edits their own sede's, owner/admin any.

The merge is tested pure; the storage, RLS and the routes against the real
database acting as mesio_app (docs/claude/testing.md).
"""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest

from app.services import sede_context
from app.services.sede_menu import apply_sede_changes, parse_price
from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)

BASE = {
    "Sopas": [
        {"name": "Ajiaco", "price": 25000},
        {"name": "Sancocho", "price": 22000},
    ],
    "Bebidas": [{"name": "Limonada", "price": 6000}],
}


# ── The merge, pure ──────────────────────────────────────────────────────────

def test_a_sede_price_replaces_the_base_price():
    menu = apply_sede_changes(BASE, [{"dish_name": "ajiaco ", "price": Decimal("27500.00"), "hidden": False}], [])
    ajiaco = next(d for d in menu["Sopas"] if d["name"] == "Ajiaco")
    assert ajiaco["price"] == 27500
    # Never a Decimal: the merged carta ends up in carts state_store serializes.
    assert not isinstance(ajiaco["price"], Decimal)
    json.dumps(menu)


def test_cents_survive_as_a_number():
    menu = apply_sede_changes(BASE, [{"dish_name": "Limonada", "price": Decimal("6500.50"), "hidden": False}], [])
    assert menu["Bebidas"][0]["price"] == 6500.5


def test_a_hidden_dish_is_gone_and_an_emptied_category_with_it():
    menu = apply_sede_changes(BASE, [{"dish_name": "Limonada", "price": None, "hidden": True}], [])
    assert "Bebidas" not in menu
    assert [d["name"] for d in menu["Sopas"]] == ["Ajiaco", "Sancocho"]


def test_an_override_without_price_keeps_the_base_price():
    menu = apply_sede_changes(BASE, [{"dish_name": "Ajiaco", "price": None, "hidden": False}], [])
    assert menu["Sopas"][0]["price"] == 25000


def test_own_dishes_join_their_category_or_start_one():
    own = [
        {"category": "Sopas", "dish": {"name": "Mote de queso", "price": 21000}},
        {"category": "Postres", "dish": {"name": "Cuajada", "price": 9000}},
    ]
    menu = apply_sede_changes(BASE, [], own)
    assert [d["name"] for d in menu["Sopas"]] == ["Ajiaco", "Sancocho", "Mote de queso"]
    assert menu["Postres"][0]["name"] == "Cuajada"
    # Normalized like any dish of the base carta.
    assert menu["Postres"][0]["active"] is True


def test_an_own_dish_wins_over_a_base_dish_added_later_with_its_name():
    own = [{"category": "Sopas", "dish": {"name": "AJIACO", "price": 30000}}]
    menu = apply_sede_changes(BASE, [], own)
    names = [d["name"].lower() for d in menu["Sopas"]]
    assert names.count("ajiaco") == 1
    assert next(d for d in menu["Sopas"] if d["name"].lower() == "ajiaco")["price"] == 30000


def test_the_base_menu_is_not_mutated():
    before = json.dumps(BASE, sort_keys=True)
    apply_sede_changes(BASE, [{"dish_name": "Ajiaco", "price": Decimal("1"), "hidden": False}],
                       [{"category": "Sopas", "dish": {"name": "X", "price": 1}}])
    assert json.dumps(BASE, sort_keys=True) == before


@pytest.mark.parametrize("raw", ["abc", "-1", "NaN", True, "", "Infinity"])
def test_parse_price_refuses_what_is_not_a_price(raw):
    with pytest.raises(ValueError):
        parse_price(raw)


def test_parse_price_rounds_to_cents():
    assert parse_price("12000.456") == Decimal("12000.46")
    assert parse_price(15000) == Decimal("15000.00")


# ── Storage, against the real database ──────────────────────────────────────

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


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


@pytest.fixture
async def raw_pool():
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    """One connection, one rolled-back transaction, acting as mesio_app so
    RLS is genuinely exercised (docs/claude/testing.md)."""
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        # Match the production pool's jsonb codec so repo code that passes
        # raw lists/dicts to $n::jsonb encodes exactly once.
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog",
        )
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


async def _pin(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))



async def _seed_org(conn, name: str, menu: dict | None = None) -> int:
    return await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu) VALUES ($1, $2, $3::jsonb) RETURNING id",
        name, f"sm-{uuid.uuid4().hex[:10]}", menu if menu is not None else BASE,
    )


async def _seed_sede(conn, org_id: int, name: str = "Sede") -> int:
    return await conn.fetchval(
        "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
        org_id, name,
    )


def _names(menu: dict) -> set[str]:
    return {d["name"] for dishes in menu.values() for d in dishes}


def _price(menu: dict, name: str):
    return next(d["price"] for dishes in menu.values() for d in dishes if d["name"] == name)


@_needs_db
async def test_each_sede_sees_its_own_carta(db_conn):
    from app.repositories import sede_menu_repo
    from app.services.sede_menu import get_sede_menu

    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    centro = await _seed_sede(db_conn, org, "Centro")
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_set_override(org, norte, "Ajiaco", price=Decimal("28000"), hidden=False)
        await sede_menu_repo.db_set_override(org, norte, "Limonada", price=None, hidden=True)
        await sede_menu_repo.db_save_own_dish(org, norte, "Postres", {"name": "Cuajada", "price": Decimal("9000")})

        n = await get_sede_menu(org, norte)
        c = await get_sede_menu(org, centro)

    assert _price(n, "Ajiaco") == 28000
    assert "Limonada" not in _names(n)
    assert "Cuajada" in _names(n)
    # Centro changed nothing: the org's carta, untouched.
    assert _price(c, "Ajiaco") == 25000
    assert _names(c) == {"Ajiaco", "Sancocho", "Limonada"}


@_needs_db
async def test_a_sede_price_survives_a_base_price_change(db_conn):
    from app.repositories import sede_menu_repo
    from app.services.sede_menu import get_sede_menu

    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_set_override(org, sede, "Ajiaco", price=Decimal("28000"), hidden=False)
        changed = json.loads(json.dumps(BASE))
        changed["Sopas"][0]["price"] = 31000
        await db_conn.execute("UPDATE organizations SET menu = $1::jsonb WHERE id = $2", changed, org)
        assert _price(await get_sede_menu(org, sede), "Ajiaco") == 28000


@_needs_db
async def test_clearing_an_override_removes_the_row(db_conn):
    from app.repositories import sede_menu_repo

    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_set_override(org, sede, "Ajiaco", price=Decimal("1"), hidden=False)
        await sede_menu_repo.db_set_override(org, sede, "AJIACO", price=Decimal("2"), hidden=False)
        rows = await sede_menu_repo.db_list_overrides(org, sede)
        assert len(rows) == 1 and rows[0]["price"] == Decimal("2.00")
        assert await sede_menu_repo.db_set_override(org, sede, "ajiaco", price=None, hidden=False) is None
        assert await sede_menu_repo.db_list_overrides(org, sede) == []


@_needs_db
async def test_an_own_dish_cannot_take_a_base_dish_name(db_conn):
    from app.repositories import sede_menu_repo
    from app.repositories.sede_menu_repo import SedeMenuError

    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        with pytest.raises(SedeMenuError):
            await sede_menu_repo.db_save_own_dish(org, sede, "Sopas", {"name": " ajiaco ", "price": 1})


@_needs_db
async def test_renaming_an_own_dish_replaces_it(db_conn):
    from app.repositories import sede_menu_repo

    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_save_own_dish(org, sede, "Postres", {"name": "Cuajada", "price": 9000})
        await sede_menu_repo.db_save_own_dish(
            org, sede, "Postres", {"name": "Cuajada con melao", "price": 9500}, previous_name="Cuajada",
        )
        rows = await sede_menu_repo.db_list_own_dishes(org, sede)
    assert [r["dish_name"] for r in rows] == ["Cuajada con melao"]
    assert rows[0]["dish"]["price"] == 9500


@_needs_db
async def test_a_sede_of_another_org_is_refused(db_conn):
    from app.repositories import sede_menu_repo
    from app.repositories.sede_menu_repo import SedeMenuError

    mine = await _seed_org(db_conn, "Mia")
    other = await _seed_org(db_conn, "Ajena")
    their_sede = await _seed_sede(db_conn, other)
    with tenant_scope(mine):
        await _pin(db_conn, mine)
        with pytest.raises(SedeMenuError):
            await sede_menu_repo.db_set_override(mine, their_sede, "Ajiaco", price=Decimal("1"), hidden=False)


@_needs_db
async def test_rls_hides_another_orgs_changes(db_conn):
    from app.repositories import sede_menu_repo

    a = await _seed_org(db_conn, "A")
    b = await _seed_org(db_conn, "B")
    sede_a = await _seed_sede(db_conn, a)
    with tenant_scope(a):
        await _pin(db_conn, a)
        await sede_menu_repo.db_set_override(a, sede_a, "Ajiaco", price=Decimal("1"), hidden=False)
    with tenant_scope(b):
        await _pin(db_conn, b)
        # Even naming A's ids, B's connection sees nothing.
        assert await db_conn.fetchval("SELECT count(*) FROM location_menu_overrides") == 0
        assert await sede_menu_repo.db_list_overrides(a, sede_a) == []


@_needs_db
async def test_the_bot_prices_a_dish_at_the_sede_of_the_turn(db_conn):
    """find_dish runs deep in the bot; it reads the turn's sede from
    sede_context, set by agent.chat once the restaurant is resolved."""
    from app.repositories import sede_menu_repo
    from app.services.orders import find_dish

    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    centro = await _seed_sede(db_conn, org, "Centro")
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_set_override(org, norte, "Ajiaco", price=Decimal("28000"), hidden=False)
        await sede_menu_repo.db_set_override(org, norte, "Sancocho", price=None, hidden=True)

        token = sede_context.begin_turn()
        try:
            sede_context.set_sede(norte)
            assert (await find_dish("ajiaco", "+570000000000"))["price"] == 28000
            assert await find_dish("Sancocho", "+570000000000") is None
            sede_context.set_sede(centro)
            assert (await find_dish("ajiaco", "+570000000000"))["price"] == 25000
            assert (await find_dish("Sancocho", "+570000000000"))["name"] == "Sancocho"
        finally:
            sede_context.end_turn(token)
    assert sede_context.current_sede_id() is None


@_needs_db
async def test_cart_resolution_refuses_a_dish_the_sede_hides(db_conn):
    from app.repositories import sede_menu_repo
    from app.services.orders import resolve_dish_for_cart

    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    centro = await _seed_sede(db_conn, org, "Centro")
    with tenant_scope(org):
        await _pin(db_conn, org)
        await sede_menu_repo.db_set_override(org, norte, "Limonada", price=None, hidden=True)
        await sede_menu_repo.db_save_own_dish(org, norte, "Postres", {"name": "Cuajada", "price": 9000})
        assert await resolve_dish_for_cart("+57", org, name="Limonada", location_id=norte) is None
        assert (await resolve_dish_for_cart("+57", org, name="Cuajada", location_id=norte))["price"] == 9000
        assert (await resolve_dish_for_cart("+57", org, name="Limonada", location_id=centro))["price"] == 6000
        assert await resolve_dish_for_cart("+57", org, name="Cuajada", location_id=centro) is None


# ── Routes: who may change which sede's carta ────────────────────────────────

def _as(monkeypatch, *, org: int, sede: int | None, role: str):
    from app.services import database as db

    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="u_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value={
        "username": "u_test", "org_id": org, "location_id": sede, "role": role,
    }))


async def _call(method: str, path: str, **kw) -> httpx.Response:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer t", **kw.pop("headers", {})}
        return await client.request(method, path, headers=headers, **kw)


@_needs_db
async def test_a_gerente_edits_their_own_sede_whatever_the_header_says(db_conn, monkeypatch):
    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    centro = await _seed_sede(db_conn, org, "Centro")
    _as(monkeypatch, org=org, sede=norte, role="gerente")

    r = await _call("PUT", "/api/menu/sede/override",
                    json={"dish_name": "Ajiaco", "price": "27000"},
                    headers={"X-Branch-ID": str(centro)})
    assert r.status_code == 200, r.text
    assert r.json()["location_id"] == norte

    with tenant_scope(org):
        await _pin(db_conn, org)
        assert await db_conn.fetchval(
            "SELECT count(*) FROM location_menu_overrides WHERE location_id = $1", centro) == 0
        assert await db_conn.fetchval(
            "SELECT price FROM location_menu_overrides WHERE location_id = $1", norte) == Decimal("27000.00")

    r = await _call("GET", "/api/menu/sede")
    assert r.status_code == 200
    body = r.json()
    assert _price(body["menu"], "Ajiaco") == 27000
    assert _price(body["base"], "Ajiaco") == 25000


@_needs_db
async def test_an_owner_edits_the_sede_they_pick(db_conn, monkeypatch):
    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    centro = await _seed_sede(db_conn, org, "Centro")
    _as(monkeypatch, org=org, sede=norte, role="owner")

    r = await _call("PUT", "/api/menu/sede/dish",
                    json={"category": "Postres", "dish": {"name": "Cuajada", "price": 9000}},
                    headers={"X-Branch-ID": str(centro)})
    assert r.status_code == 200, r.text
    assert r.json()["location_id"] == centro


@_needs_db
async def test_a_cashier_cannot_change_the_carta(db_conn, monkeypatch):
    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    _as(monkeypatch, org=org, sede=sede, role="caja")
    r = await _call("PUT", "/api/menu/sede/override", json={"dish_name": "Ajiaco", "hidden": True})
    assert r.status_code == 403


@_needs_db
async def test_a_gerente_cannot_edit_the_base_carta(db_conn, monkeypatch):
    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    _as(monkeypatch, org=org, sede=sede, role="gerente")
    r = await _call("PUT", "/api/menu/update", json={"menu": BASE})
    assert r.status_code == 403


@_needs_db
async def test_hiding_a_dish_the_base_does_not_have_is_404(db_conn, monkeypatch):
    org = await _seed_org(db_conn, "Cadena")
    sede = await _seed_sede(db_conn, org)
    _as(monkeypatch, org=org, sede=sede, role="gerente")
    r = await _call("PUT", "/api/menu/sede/override", json={"dish_name": "Pizza", "hidden": True})
    assert r.status_code == 404
    r = await _call("PUT", "/api/menu/sede/override", json={"dish_name": "Ajiaco", "price": "abc"})
    assert r.status_code == 400


@_needs_db
async def test_a_pin_login_gerente_gets_their_sede_carta_on_inventory_screens(db_conn, monkeypatch):
    """A PIN-login employee resolved twice in one request — once to pin the
    tenant, once inside it to pick the sede — used to open a bypass inside
    the pinned scope: TenantContextConflict, a 500 on every inventory call.
    The user is now resolved once per request."""
    from app.repositories import sede_menu_repo

    org = await _seed_org(db_conn, "Cadena")
    norte = await _seed_sede(db_conn, org, "Norte")
    with tenant_scope(org):
        await _pin(db_conn, org)
        staff_id = await db_conn.fetchval(
            """INSERT INTO staff (id, org_id, location_id, name, username, role, roles, pin, active)
               VALUES (gen_random_uuid(), $1, $2, 'Gerente', $3, 'gerente', '["gerente"]'::jsonb, 'x', TRUE)
               RETURNING id""",
            org, norte, f"g-{uuid.uuid4().hex[:8]}",
        )
        await sede_menu_repo.db_save_own_dish(org, norte, "Postres", {"name": "Cuajada", "price": 9000})
        await sede_menu_repo.db_set_override(org, norte, "Limonada", price=None, hidden=True)
    await db_conn.execute("SELECT set_config('app.org_id', '', true)")

    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=f"staff:{staff_id}"))
    r = await _call("GET", "/api/inventory/menu-items")
    assert r.status_code == 200, r.text
    names = {d["name"] for d in r.json()["dishes"]}
    assert "Cuajada" in names
    assert "Limonada" not in names
