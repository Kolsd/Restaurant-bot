"""
tests/test_table_payment_options.py
===================================
Paying at the table with what THIS sede accepts (app/services/payment_options.py).

Before 2026-10-01 the table chat only ever offered tarjeta/efectivo, whatever
the sede took, and /pedir asked for a Nequi receipt without saying where to
send the money. Now both read the sede's own methods and the restaurant's
transfer instructions; a transfer at the table needs the receipt first, and
the check lands in Caja › Comprobantes as proof_received.

Only Cloudinary is faked (image_host.upload_delivery_proof); everything else
goes through the real routes and DB.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from app.services import payment_options

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped")

NEQUI_TEXT = "Nequi 300 123 4567 a nombre de Arepas Juan"


# ── Unit ─────────────────────────────────────────────────────────────────────

def test_transfer_instructions_only_keep_real_text_and_old_capitalised_keys():
    got = payment_options.transfer_instructions({"payment_instructions": {
        "Nequi": "  " + NEQUI_TEXT + "  ", "bancolombia": "   ", "daviplata": "no aplica",
    }})
    assert got == {"nequi": NEQUI_TEXT}


@pytest.mark.parametrize("key,kind", [("efectivo", "cash"), ("tarjeta", "card"),
                                      ("nequi", "transfer"), ("bancolombia", "transfer")])
def test_kind_of(key, kind):
    assert payment_options.kind_of(key) == kind


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


def _post(client, url, **kw):
    _reset_pool()
    return client.post(url, **kw)


def _get(client, url, **kw):
    _reset_pool()
    return client.get(url, **kw)


async def _scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))


async def _seed(methods: list | None) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        menu = {"Principales": [{"name": "Bandeja Paisa", "price": 28000, "active": True, "sku": "bandeja"}]}
        features = {"currency": "COP", "payment_instructions": {"nequi": NEQUI_TEXT}}
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, menu, features) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
            f"Pay Org {suffix}", f"pay-{suffix}", json.dumps(menu), json.dumps(features),
        )
        await _scope(conn, org_id)
        cfg = {"payment_methods": methods} if methods is not None else {}
        loc = await conn.fetchval(
            "INSERT INTO locations (org_id, name, delivery_config) VALUES ($1, $2, $3::jsonb) RETURNING id",
            org_id, f"Sede {suffix}", json.dumps(cfg),
        )
        table_id = f"t-{suffix}"
        await conn.execute(
            "INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active) "
            "VALUES ($1, 1, '1', $2, $3, $4, TRUE)",
            table_id, loc, loc, org_id,
        )
        return {"org_id": org_id, "location_id": loc, "table_id": table_id}
    finally:
        await conn.close()


async def _teardown(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        await conn.execute("DELETE FROM waiter_alerts WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def nequi_sede():
    info = _run(_seed(["efectivo", "nequi"]))
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


@pytest.fixture
def plain_sede():
    info = _run(_seed(None))
    try:
        yield info
    finally:
        _run(_teardown(info["org_id"]))


def _seated_with_order(client, table_id: str) -> str:
    token = _post(client, "/api/diner/session", json={"table_id": table_id}).json()["token"]
    assert _post(client, "/api/diner/cart/add", json={"token": token, "sku": "bandeja", "qty": 1}).status_code == 200
    assert _post(client, "/api/diner/order/send", json={"token": token, "idempotency_key": "k"}).status_code == 200
    return token


def _checkout(client, token, method):
    return _post(client, "/api/diner/checkout",
                 json={"token": token, "scope": "mine", "method": method, "tip_amount": 0})


async def _check_and_alert(org_id: int):
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _scope(conn, org_id)
        check = await conn.fetchrow(
            "SELECT proposal_status, proof_media_url, proposed_payments FROM table_checks c "
            "JOIN table_orders o ON o.base_order_id = c.base_order_id WHERE o.org_id = $1 LIMIT 1", org_id,
        )
        alert = await conn.fetchval(
            "SELECT message FROM waiter_alerts WHERE org_id = $1 ORDER BY id DESC LIMIT 1", org_id,
        )
        return dict(check) if check else None, alert
    finally:
        await conn.close()


@needs_db
def test_a_sede_without_methods_keeps_tarjeta_and_efectivo(client, plain_sede):
    token = _post(client, "/api/diner/session", json={"table_id": plain_sede["table_id"]}).json()["token"]
    methods = _get(client, f"/api/diner/payment-options?token={token}").json()["methods"]
    assert [(m["key"], m["kind"]) for m in methods] == [("tarjeta", "card"), ("efectivo", "cash")]


@needs_db
def test_the_table_offers_what_the_sede_accepts_with_where_to_transfer(client, nequi_sede):
    token = _post(client, "/api/diner/session", json={"table_id": nequi_sede["table_id"]}).json()["token"]
    methods = _get(client, f"/api/diner/payment-options?token={token}").json()["methods"]
    assert methods == [
        {"key": "efectivo", "label": "Efectivo", "kind": "cash", "instructions": ""},
        {"key": "nequi", "label": "Nequi", "kind": "transfer", "instructions": NEQUI_TEXT},
    ]


@needs_db
def test_a_method_the_sede_does_not_take_is_refused(client, nequi_sede):
    token = _seated_with_order(client, nequi_sede["table_id"])
    resp = _checkout(client, token, "card")  # legacy key for tarjeta; this sede has no datáfono
    assert resp.status_code == 422
    assert "medio de pago" in resp.json()["detail"]


@needs_db
def test_a_transfer_needs_the_receipt_and_lands_in_comprobantes(client, nequi_sede, monkeypatch):
    from app.services import image_host
    monkeypatch.setattr(image_host, "upload_delivery_proof",
                        lambda org_id, data, ctype: {"secure_url": "https://img.test/proof-1.jpg"})
    token = _seated_with_order(client, nequi_sede["table_id"])

    early = _checkout(client, token, "nequi")
    assert early.status_code == 422
    assert "comprobante" in early.json()["detail"]

    _reset_pool()
    up = client.post("/api/diner/delivery/payment-proof", data={"token": token},
                     files={"file": ("p.jpg", b"\xff\xd8\xff fake", "image/jpeg")})
    assert up.status_code == 200, up.text

    resp = _checkout(client, token, "nequi")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 28000
    assert resp.json()["message"].startswith("Recibimos tu comprobante")

    check, alert = _run(_check_and_alert(nequi_sede["org_id"]))
    assert check["proposal_status"] == "proof_received"
    assert check["proof_media_url"] == "https://img.test/proof-1.jpg"
    payments = check["proposed_payments"]
    payments = json.loads(payments) if isinstance(payments, str) else payments
    assert payments == [{"method": "nequi", "amount": 28000.0}]
    assert "Nequi" in alert and "Comprobantes" in alert


@needs_db
def test_cash_still_just_calls_the_waiter(client, nequi_sede):
    token = _seated_with_order(client, nequi_sede["table_id"])
    resp = _checkout(client, token, "efectivo")
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Ya le avisamos al mesero, ya viene con tu cuenta."
    check, alert = _run(_check_and_alert(nequi_sede["org_id"]))
    assert check["proposal_status"] == "pending" and check["proof_media_url"] is None
    assert "Efectivo" in alert and "Comprobantes" not in alert
