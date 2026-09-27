"""A restaurant born from self-serve signup has no WhatsApp number.

Every diner test seeds `locations.whatsapp_number`, so none of them notice
when a web-channel flow still demands one. This file provisions the org the
way `/api/signup` does (`create_tenant`, no number) and drives the flows a
diner hits first: scanning the table QR and ordering from `/pedir`.
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


async def _provision() -> dict:
    from app.services import provisioning

    suffix = uuid.uuid4().hex[:8]
    tenant = await provisioning.create_tenant(
        restaurant_name=f"Self Serve {suffix}",
        username=f"selfserve{suffix}",
        password="una-clave-larga-123",
        owner_email=f"owner{suffix}@example.com",
        send_welcome_email=False,
        allow_username_suffix=False,
    )
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        org_id = int(tenant.org["id"])
        location_id = await conn.fetchval(
            "SELECT id FROM locations WHERE org_id = $1 ORDER BY id LIMIT 1", org_id,
        )
        assert await conn.fetchval(
            "SELECT COALESCE(l.whatsapp_number, o.whatsapp_number) FROM locations l "
            "JOIN organizations o ON o.id = l.org_id WHERE l.id = $1", location_id,
        ) is None, "precondition: a self-serve sede has no WhatsApp number"
        # 0096: the view gives such an org a web key instead of NULL.
        assert await conn.fetchval(
            "SELECT whatsapp_number FROM restaurants WHERE id = $1", location_id,
        ) == f"web{org_id}"
        # The owner builds the carta and switches pickup on from the panel;
        # seed the result of that directly.
        menu = {"Principales": [
            {"name": "Ajiaco", "description": "", "price": 22000, "active": True, "sku": "ajiaco"},
        ]}
        await conn.execute(
            "UPDATE organizations SET menu = $2::jsonb WHERE id = $1", org_id, json.dumps(menu),
        )
        await conn.execute(
            "UPDATE locations SET delivery_config = $2::jsonb WHERE id = $1",
            location_id, json.dumps({"delivery_enabled": False, "pickup_enabled": True}),
        )
        slug = await conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_id)
        table_id = f"t-{suffix}"
        await conn.execute(
            "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
            "VALUES ($1, 1, 'Mesa 1', $2, $3, $4, TRUE)",
            table_id, location_id, location_id, org_id,
        )
    finally:
        await conn.close()
    _reset_pool()
    return {
        "org_id": org_id, "location_id": location_id, "table_id": table_id, "slug": slug,
        "username": tenant.username,
    }


async def _drop(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM users WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def self_serve_org():
    info = _run(_provision())
    try:
        yield info
    finally:
        _run(_drop(info["org_id"]))


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


async def _fetch(sql: str, *args):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def test_table_qr_order_reaches_the_kitchen_without_whatsapp(client, self_serve_org):
    org_id = self_serve_org["org_id"]

    resp = _post(client, "/api/diner/session", json={"table_id": self_serve_org["table_id"]})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]

    resp = _post(client, "/api/diner/cart/add", json={"token": token, "sku": "ajiaco", "qty": 2})
    assert resp.status_code == 200, resp.text
    resp = _post(client, "/api/diner/order/send", json={
        "token": token, "idempotency_key": str(uuid.uuid4()),
    })
    assert resp.status_code == 200, resp.text

    orders = _run(_fetch(
        "SELECT items FROM table_orders WHERE table_id = $1", self_serve_org["table_id"],
    ))
    assert len(orders) == 1
    # Raw asyncpg has no jsonb codec: a correctly stored array comes back as
    # its JSON text, a double-encoded one as a quoted string.
    items = json.loads(orders[0]["items"])
    assert isinstance(items, list), f"items stored as a JSON string: {orders[0]['items'][:80]}"
    assert [(i["name"], i.get("qty") or i.get("quantity")) for i in items] == [("Ajiaco", 2)]

    # The session carries the org's web key, and it is the same key the
    # owner's NPS stats read from the view — otherwise ratings are saved
    # under one key and looked up under another.
    sessions = _run(_fetch(
        "SELECT DISTINCT bot_number FROM diner_sessions WHERE org_id = $1", org_id,
    ))
    view_key = _run(_fetch(
        "SELECT whatsapp_number FROM restaurants WHERE id = $1", self_serve_org["location_id"],
    ))[0]["whatsapp_number"]
    assert [r["bot_number"] for r in sessions] == [f"web{org_id}"] == [view_key]


def test_pickup_from_pedir_opens_without_whatsapp(client, self_serve_org):
    resp = _post(client, "/api/diner/session", json={
        "order_mode": "pickup",
        "slug": self_serve_org["slug"],
        "location_id": self_serve_org["location_id"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"].startswith("web:")


async def _owner_token(username: str) -> str:
    from app.repositories.sessions_repo import create_session
    return await create_session(username)


def test_owner_creates_a_sede_inside_their_own_org(client, self_serve_org):
    """POST /api/team/branches created a brand-new ORGANIZATION (a copy of the
    owner's menu and features) and then tried to re-parent its sede by a
    WhatsApp number that sede was never given — so it matched nothing and the
    owner never saw the sede they created (found 2026-09-25)."""
    org_id = self_serve_org["org_id"]
    _reset_pool()
    token = _run(_owner_token(self_serve_org["username"]))
    headers = {"Authorization": f"Bearer {token}"}

    orgs_before = _run(_fetch("SELECT count(*) AS n FROM organizations"))[0]["n"]

    _reset_pool()
    resp = client.post("/api/team/branches", headers=headers, json={
        "name": "Sede Norte", "address": "Calle 1 # 2-3",
        "latitude": 4.7, "longitude": -74.05,
    })
    assert resp.status_code == 200, resp.text

    orgs_after = _run(_fetch("SELECT count(*) AS n FROM organizations"))[0]["n"]
    assert orgs_after == orgs_before, "creating a sede must not create an organization"

    sedes = _run(_fetch(
        "SELECT name, latitude FROM locations WHERE org_id = $1 ORDER BY id", org_id,
    ))
    assert [r["name"] for r in sedes][-1] == "Sede Norte"
    assert len(sedes) == 2

    _reset_pool()
    listed = client.get("/api/team/branches", headers=headers)
    assert listed.status_code == 200, listed.text
    names = [b.get("location_name") or b.get("name") for b in listed.json()["branches"]]
    assert "Sede Norte" in names
