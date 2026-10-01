"""
tests/test_diner_memory.py
==========================
Diner memory (migration 0104, app/services/diner_memory.py): with consent,
a browser is remembered across visits and across every sede of the org.

Unit tests cover the pure pieces (key hashing, history aggregation). The
integration tests drive the real routes against TEST_DATABASE_URL with an
org of TWO sedes, because "per organization" is the PM's call: an order at
sede A is "lo de siempre" at sede B, while what can be ordered still comes
from sede B.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from app.services import diner_memory

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")

MEMORY_KEY = "a" * 16 + uuid.uuid4().hex  # 48 chars, like the browser's 24 random bytes in hex


# ── Unit: key ────────────────────────────────────────────────────────────────

def test_device_key_hashes_and_never_returns_the_secret():
    key = diner_memory.device_key(MEMORY_KEY)
    assert key.startswith("device:")
    assert MEMORY_KEY not in key
    assert len(key) == len("device:") + 64
    assert diner_memory.device_key(MEMORY_KEY) == key  # stable across visits


@pytest.mark.parametrize("raw", [None, "", "short-key", "x" * 31, "x" * 201])
def test_device_key_rejects_guessable_or_oversized_secrets(raw):
    assert diner_memory.device_key(raw) is None


# ── Unit: aggregation ────────────────────────────────────────────────────────

def _row(token, items):
    return {"session_token": token, "created_at": None, "items": json.dumps(items)}


def test_aggregate_empty_history():
    assert diner_memory.aggregate_history([]) == {"visits": 0, "last_visit": [], "favorites": []}


def test_aggregate_single_visit_merges_rounds_and_keeps_notes_but_has_no_favorites():
    rows = [  # newest first, as the repo returns them
        _row("web:v1", [{"name": "Bandeja Paisa", "quantity": 1, "note": "sin cebolla"}]),
        _row("web:v1", [
            {"name": "Bandeja Paisa", "quantity": 1, "note": "sin cebolla"},
            {"name": "Limonada", "quantity": 2},
        ]),
    ]
    out = diner_memory.aggregate_history(rows)
    assert out["visits"] == 1
    assert out["last_visit"] == [
        {"name": "Bandeja Paisa", "qty": 2, "note": "sin cebolla"},
        {"name": "Limonada", "qty": 2, "note": ""},
    ]
    assert out["favorites"] == []


def test_aggregate_favorites_rank_by_visits_then_recency():
    rows = [
        _row("web:v3", [{"name": "Ajiaco", "quantity": 1}, {"name": "Limonada", "quantity": 1}]),
        _row("web:v2", [{"name": "Bandeja Paisa", "quantity": 5}, {"name": "Limonada", "quantity": 1}]),
        _row("web:v1", [{"name": "Bandeja Paisa", "quantity": 1}, {"name": "Ajiaco", "quantity": 1},
                        {"name": "Limonada", "quantity": 1}]),
    ]
    out = diner_memory.aggregate_history(rows)
    assert out["visits"] == 3
    assert out["last_visit"] == [
        {"name": "Ajiaco", "qty": 1, "note": ""},
        {"name": "Limonada", "qty": 1, "note": ""},
    ]
    # Limonada: 3 visits. Ajiaco and Bandeja: 2 each — Ajiaco was ordered more recently.
    assert out["favorites"] == [
        {"name": "Limonada", "count": 3},
        {"name": "Ajiaco", "count": 2},
        {"name": "Bandeja Paisa", "count": 2},
    ]


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


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


async def _scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))


async def _make_org(conn) -> dict:
    suffix = uuid.uuid4().hex[:10]
    menu = {"Principales": [
        {"name": "Bandeja Paisa", "description": "", "price": 28000, "active": True, "sku": "bandeja"},
        {"name": "Ajiaco", "description": "", "price": 22000, "active": True, "sku": "ajiaco"},
    ]}
    org_id = await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu, features) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
        f"Memory Org {suffix}", f"memory-{suffix}", json.dumps(menu), json.dumps({"currency": "COP"}),
    )
    await _scope(conn, org_id)
    sedes = []
    for n in (1, 2):
        location_id = await conn.fetchval(
            "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id", org_id, f"Sede {n} {suffix}",
        )
        table_id = f"t{n}-{suffix}"
        await conn.execute(
            "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
            "VALUES ($1, $2, $3, $4, $5, $6, TRUE)",
            table_id, n, str(n), location_id, location_id, org_id,
        )
        sedes.append({"location_id": location_id, "table_id": table_id})
    return {"org_id": org_id, "sedes": sedes}


async def _seed() -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await _make_org(conn)
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM customer_profiles WHERE org_id = $1", org_id)
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
def other_org():
    info = _run(_seed())
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


async def _fetch(sql: str, org_id: int, *args):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        return [dict(r) for r in await conn.fetch(sql, org_id, *args)]
    finally:
        await conn.close()


def _leave(org_id: int) -> None:
    """Everyone gets up: the table sessions close, as when the waiter closes
    the table. The next scan is a new visit at a free table."""
    async def _close():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await _scope(conn, org_id)
            await conn.execute(
                "UPDATE table_sessions SET status = 'closed', closed_at = NOW() "
                "WHERE org_id = $1 AND status = 'active'", org_id,
            )
        finally:
            await conn.close()
    _run(_close())


def _open(client, table_id: str, memory_key: str | None = None) -> dict:
    body = {"table_id": table_id}
    if memory_key:
        body["memory_key"] = memory_key
    resp = _post(client, "/api/diner/session", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add(client, token: str, sku: str, qty: int = 1, note: str | None = None) -> None:
    body = {"token": token, "sku": sku, "qty": qty}
    if note:
        body["note"] = note
    resp = _post(client, "/api/diner/cart/add", json=body)
    assert resp.status_code == 200, resp.text


def _send(client, token: str) -> dict:
    resp = _post(client, "/api/diner/order/send", json={"token": token, "idempotency_key": uuid.uuid4().hex})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _consent(client, token: str, key: str = MEMORY_KEY):
    return _post(client, "/api/diner/memory/consent", json={"token": token, "memory_key": key})


def _first_visit(client, org: dict) -> dict:
    """Sede 1: 2× Bandeja (sin cebolla) + Ajiaco, sent, then "Sí, recuérdame"."""
    s = _open(client, org["sedes"][0]["table_id"])
    _add(client, s["token"], "bandeja", 2, "sin cebolla")
    _add(client, s["token"], "ajiaco", 1)
    sent = _send(client, s["token"])
    assert sent["memory_offer"] is True
    resp = _consent(client, s["token"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["remembered"] is True
    return s


@needs_db
def test_a_stranger_is_offered_memory_and_nothing_is_stored(client, org):
    s = _open(client, org["sedes"][0]["table_id"])
    assert s["remembered"] is False
    assert "Hola de nuevo" not in s["message"]
    _add(client, s["token"], "bandeja")
    assert _send(client, s["token"])["memory_offer"] is True

    profiles = _run(_fetch("SELECT id FROM customer_profiles WHERE org_id = $1", org["org_id"], ))
    assert profiles == []


@needs_db
def test_remembered_at_another_sede_and_repeats_the_last_order(client, org):
    _first_visit(client, org)
    org_id = org["org_id"]
    sede2 = org["sedes"][1]

    stored = _run(_fetch("SELECT phone, consent_at FROM customer_profiles WHERE org_id = $1", org_id))
    assert len(stored) == 1
    assert stored[0]["phone"] == diner_memory.device_key(MEMORY_KEY)  # a hash, never the secret
    assert stored[0]["consent_at"] is not None

    back = _open(client, sede2["table_id"], MEMORY_KEY)
    assert back["remembered"] is True
    assert back["message"] == "¡Hola de nuevo! La última vez pediste 2× Bandeja Paisa, Ajiaco."
    actions = [b for b in back["blocks"] if b["type"] == "memory_actions"]
    assert actions == [{
        "type": "memory_actions", "can_repeat": True,
        "repeat_label": "Repetir mi último pedido", "unavailable": [],
    }]
    # The usual category chips still follow the memory block.
    assert back["blocks"][-1]["type"] == "category_chips"
    # Already remembered: no second "¿te recordamos?".
    _add(client, back["token"], "ajiaco")
    assert _send(client, back["token"])["memory_offer"] is False

    third = _open(client, sede2["table_id"], MEMORY_KEY)
    resp = _post(client, "/api/diner/memory/repeat", json={"token": third["token"]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # The last visit is the second one (1× Ajiaco), not the first.
    assert data["added"] == ["Ajiaco"] and data["skipped"] == []
    cart = data["blocks"][0]
    assert [(i["name"], i["qty"]) for i in cart["items"]] == [("Ajiaco", 1)]
    assert cart["subtotal"] == 22000


@needs_db
def test_repeat_restores_notes_and_skips_what_this_sede_ran_out_of(client, org):
    _first_visit(client, org)
    sede2 = org["sedes"][1]
    conn_sql = (
        "INSERT INTO menu_availability (org_id, location_id, dish_name, available) "
        "VALUES ($1, $2, 'Ajiaco', FALSE)"
    )

    async def _sold_out():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await _scope(conn, org["org_id"])
            await conn.execute(conn_sql, org["org_id"], sede2["location_id"])
        finally:
            await conn.close()
    _run(_sold_out())

    back = _open(client, sede2["table_id"], MEMORY_KEY)
    actions = next(b for b in back["blocks"] if b["type"] == "memory_actions")
    assert actions["can_repeat"] is True
    assert actions["unavailable"] == ["Ajiaco"]

    data = _post(client, "/api/diner/memory/repeat", json={"token": back["token"]}).json()
    assert data["added"] == ["Bandeja Paisa"]
    assert data["skipped"] == ["Ajiaco"]
    assert "Ajiaco" in data["message"]
    items = data["blocks"][0]["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Bandeja Paisa" and items[0]["qty"] == 2
    assert items[0]["note"] == "sin cebolla"
    assert data["blocks"][0]["subtotal"] == 56000  # today's price, from the sede menu


@needs_db
def test_favorites_and_bot_context_after_two_visits(client, org):
    _first_visit(client, org)
    _leave(org["org_id"])
    back = _open(client, org["sedes"][1]["table_id"], MEMORY_KEY)
    _add(client, back["token"], "bandeja")
    _send(client, back["token"])

    _leave(org["org_id"])

    third = _open(client, org["sedes"][0]["table_id"], MEMORY_KEY)
    cards = [b for b in third["blocks"] if b["type"] == "dish_cards"]
    assert [d["name"] for d in cards[0]["dishes"]] == ["Bandeja Paisa"]

    from app.services.tenant_context import tenant_scope

    async def _ctx():
        _reset_pool()
        with tenant_scope(org["org_id"]):
            return await diner_memory.prompt_context(third["token"], org["org_id"])
    ctx, favorites = _run(_ctx())
    assert favorites == [{"name": "Bandeja Paisa", "count": 2}]
    assert "2 pedidos previos" in ctx
    assert "Último: Bandeja Paisa" in ctx


@needs_db
def test_forget_deletes_the_profile_and_unlinks_every_session(client, org):
    first = _first_visit(client, org)
    _leave(org["org_id"])
    back = _open(client, org["sedes"][1]["table_id"], MEMORY_KEY)
    assert back["remembered"] is True

    # A session token alone (another diner at the table) can't erase it.
    wrong = _post(client, "/api/diner/memory/forget",
                  json={"token": back["token"], "memory_key": "b" * 48})
    assert wrong.status_code == 200
    assert len(_run(_fetch("SELECT id FROM customer_profiles WHERE org_id = $1", org["org_id"]))) == 1

    resp = _post(client, "/api/diner/memory/forget", json={"token": back["token"], "memory_key": MEMORY_KEY})
    assert resp.status_code == 200, resp.text
    assert resp.json()["remembered"] is False
    assert _run(_fetch("SELECT id FROM customer_profiles WHERE org_id = $1", org["org_id"])) == []
    linked = _run(_fetch(
        "SELECT token FROM diner_sessions WHERE org_id = $1 AND customer_profile_id IS NOT NULL", org["org_id"],
    ))
    assert linked == []
    assert first["token"]  # the old visit's session still exists, just unlinked

    _leave(org["org_id"])

    again = _open(client, org["sedes"][1]["table_id"], MEMORY_KEY)
    assert again["remembered"] is False


@needs_db
def test_memory_never_crosses_organizations(client, org, other_org):
    _first_visit(client, org)
    elsewhere = _open(client, other_org["sedes"][0]["table_id"], MEMORY_KEY)
    assert elsewhere["remembered"] is False
    assert "Hola de nuevo" not in elsewhere["message"]
    resp = _post(client, "/api/diner/memory/repeat", json={"token": elsewhere["token"]})
    assert resp.status_code == 404


@needs_db
def test_consent_rejects_a_guessable_key(client, org):
    s = _open(client, org["sedes"][0]["table_id"])
    resp = _consent(client, s["token"], key="1234")
    assert resp.status_code == 422
    assert _run(_fetch("SELECT id FROM customer_profiles WHERE org_id = $1", org["org_id"])) == []


@needs_db
def test_consent_without_orders_yet_greets_normally_next_time(client, org):
    s = _open(client, org["sedes"][0]["table_id"])
    assert _consent(client, s["token"]).status_code == 200
    _leave(org["org_id"])
    back = _open(client, org["sedes"][1]["table_id"], MEMORY_KEY)
    assert back["remembered"] is True
    assert back["message"].startswith("¡Hola! Bienvenido")
    assert not [b for b in back["blocks"] if b["type"] == "memory_actions"]
