"""
tests/test_diner_checkout.py
==============================
Integration coverage for the waiter-mediated diner web-checkout wave
(app/routes/diner.py POST /api/diner/checkout, GET /api/diner/status) and the
web-identity NPS fix (app/routes/tables.py::_farewell_and_nps).

Product decisions under test (see CLAUDE.md "WHAT TO BUILD" — no payment
gateway, waiter-mediated payment):
  - scope="mine"  → a check built from THIS diner's own table_orders rows,
    with the REAL items (never agent_salon._save_checkout_proposal's even
    "Parte N" split).
  - scope="table" → the REMAINING unpaid/un-proposed balance, never a fresh
    full total.
  - An item already inside an open/paying/invoiced check can never land in
    another check (tracked via an additive `order_id` tag on check items —
    see app/routes/diner.py::_claimed_order_ids).
  - Concurrent checkout attempts on the same table are serialized by
    state_store.table_checkout_lock_acquire (keyed by base_order_id) — the
    DB never ends up with more than one check/alert for the same balance.
  - Every amount is computed server-side from real table_orders rows —
    the client never gets to name a price.
  - Tip capped at 50% of the check subtotal (mirrors the existing pay_check
    rule).
  - The proposal must show up in tables_repo.db_list_checkout_proposals for
    the diner's own org, and never for an unrelated org.
  - NPS: 1-5 stars + "No calificar" skip, comment asked only for score<=3,
    and — the bug this wave fixes — no WhatsApp push is ever attempted for
    a "web:<uuid4>" identity.

Requires TEST_DATABASE_URL. Follows the exact pattern established in
tests/test_diner_order_flow.py (throwaway event loop for seed/teardown,
_reset_pool() before every single HTTP call through TestClient).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock

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

async def _make_org(conn) -> dict:
    suffix = uuid.uuid4().hex[:10]
    bot_number = f"573{suffix[:9]}"
    menu = {
        "Principales": [
            {"name": "Bandeja Paisa", "description": "", "price": 28000, "active": True, "sku": "bandeja"},
            {"name": "Ajiaco", "description": "", "price": 22000, "active": True, "sku": "ajiaco"},
        ],
    }
    org_id = await conn.fetchval(
        "INSERT INTO organizations (name, slug, menu, features) "
        "VALUES ($1, $2, $3::jsonb, $4::jsonb) RETURNING id",
        f"Checkout Org {suffix}", f"checkout-{suffix}",
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
    return {
        "org_id": org_id,
        "location_id": location_id,
        "table_id": table_id,
        "bot_number": bot_number,
    }


async def _drop_org(conn, org_id: int) -> None:
    await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)


async def _seed() -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await _make_org(conn)
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


# ── Small helpers ────────────────────────────────────────────────────────────

def _open(client, table_id: str) -> dict:
    resp = _post(client, "/api/diner/session", json={"table_id": table_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add(client, token: str, sku: str, qty: int = 1):
    resp = _post(client, "/api/diner/cart/add", json={"token": token, "sku": sku, "qty": qty})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _send(client, token: str):
    resp = _post(client, "/api/diner/order/send", json={
        "token": token, "idempotency_key": str(uuid.uuid4()),
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _checkout(client, token: str, scope: str, method: str = "card", tip_amount: float = 0.0, **extra):
    body = {"token": token, "scope": scope, "method": method, "tip_amount": tip_amount}
    body.update(extra)
    return _post(client, "/api/diner/checkout", json=body)


def _status(client, token: str):
    return _get(client, "/api/diner/status", params={"token": token})


async def _checks_async(org_id: int, base_order_id: str) -> list:
    from app.repositories import tables_repo
    from app.services.tenant_context import tenant_scope
    with tenant_scope(org_id):
        return await tables_repo.db_get_checks(base_order_id)


def _checks(org_id: int, base_order_id: str) -> list:
    _reset_pool()
    return _run(_checks_async(org_id, base_order_id))


async def _base_order_id_async(org_id: int, table_id: str):
    from app.repositories import tables_repo
    from app.services.tenant_context import tenant_scope
    with tenant_scope(org_id):
        return await tables_repo.db_get_base_order_id(table_id)


def _base_order_id(org_id: int, table_id: str):
    _reset_pool()
    return _run(_base_order_id_async(org_id, table_id))


async def _proposals_async(org_id: int) -> list:
    from app.repositories import tables_repo
    from app.services.tenant_context import tenant_scope
    with tenant_scope(org_id):
        return await tables_repo.db_list_checkout_proposals(org_id)


def _proposals(org_id: int) -> list:
    _reset_pool()
    return _run(_proposals_async(org_id))


async def _alerts_async(org_id: int, bot_number: str) -> list:
    from app.repositories import tables_repo
    from app.services.tenant_context import tenant_scope
    with tenant_scope(org_id):
        return await tables_repo.db_get_waiter_alerts(bot_number)


def _alerts(org_id: int, bot_number: str) -> list:
    _reset_pool()
    return _run(_alerts_async(org_id, bot_number))


def _mock_pay_auth(monkeypatch, org_id: int, bot_number: str):
    """Mirrors tests/test_split_checks.py::_mock_auth — mocks ONLY the
    caja/admin auth resolution (get_current_restaurant) so pay_check runs
    its REAL DB logic (real claim/finalize/tenant_scope) against the seeded
    org. features={} skips the DIAN path (no fiscal invoice required)."""
    async def mock_get_restaurant(request):
        return {"id": org_id, "whatsapp_number": bot_number, "name": "Test Org", "features": {}}
    monkeypatch.setattr("app.routes.tables.get_current_restaurant", mock_get_restaurant)


def _pay_check(client, base_order_id: str, check_id: str, amount: float, tip_amount: float = 0.0):
    return _post(
        client,
        f"/api/table-orders/{base_order_id}/checks/{check_id}/pay",
        json={"payments": [{"method": "efectivo", "amount": amount}], "tip_amount": tip_amount},
        headers={"Authorization": "Bearer fake"},
    )


# ── 1. "mine" charges exactly that diner's own items ────────────────────────

def test_checkout_mine_charges_only_own_items(client, org_a):
    table_id = org_a["table_id"]
    session_a = _open(client, table_id)
    token_a = session_a["token"]
    join_code = session_a["join_code"]

    session_b = _open(client, table_id)
    token_b = session_b["token"]
    _post(client, "/api/diner/join", json={"token": token_b, "code": join_code})

    _add(client, token_a, "bandeja", qty=1)  # 28000
    _send(client, token_a)
    _add(client, token_b, "ajiaco", qty=2)  # 44000
    _send(client, token_b)

    resp = _checkout(client, token_a, scope="mine", method="card")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["scope"] == "mine"
    assert data["subtotal"] == 28000
    assert data["total"] == 28000
    assert data["status"] == "pending_waiter"

    base_order_id = data["base_order_id"]
    checks = _checks(org_a["org_id"], base_order_id)
    assert len(checks) == 1
    items = checks[0]["items"]
    if isinstance(items, str):
        items = json.loads(items)
    names = {i["name"] for i in items}
    assert names == {"Bandeja Paisa"}  # never diner B's Ajiaco
    assert checks[0]["proposal_customer_phone"] == token_a
    assert checks[0]["proposal_source"] == "web_chat"
    assert checks[0]["proposal_status"] == "pending"


# ── 2. "table" charges exactly the REMAINING balance after someone paid ────

def test_checkout_table_scope_excludes_already_claimed_items(client, org_a):
    table_id = org_a["table_id"]
    session_a = _open(client, table_id)
    token_a = session_a["token"]
    join_code = session_a["join_code"]
    session_b = _open(client, table_id)
    token_b = session_b["token"]
    _post(client, "/api/diner/join", json={"token": token_b, "code": join_code})

    _add(client, token_a, "bandeja", qty=1)  # 28000
    _send(client, token_a)
    _add(client, token_b, "ajiaco", qty=1)  # 22000
    _send(client, token_b)

    # A claims their own bill first.
    resp_a = _checkout(client, token_a, scope="mine", method="card")
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json()["subtotal"] == 28000

    # B now asks for "toda la mesa" — must be ONLY the remaining 22000
    # (their own Ajiaco), never A's 28000 re-included in a fresh full total.
    resp_b = _checkout(client, token_b, scope="table", method="cash")
    assert resp_b.status_code == 200, resp_b.text
    data_b = resp_b.json()
    assert data_b["subtotal"] == 22000
    assert data_b["total"] == 22000

    base_order_id = resp_a.json()["base_order_id"]
    checks = _checks(org_a["org_id"], base_order_id)
    assert len(checks) == 2  # A's check + B's remainder check, never merged/re-billed


# ── 3. An item cannot be charged twice (double-tap idempotency) ────────────

def test_double_checkout_same_diner_is_idempotent_no_second_check(client, org_a):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)

    first = _checkout(client, token, scope="mine", method="card")
    assert first.status_code == 200, first.text
    check_id_1 = first.json()["check_id"]

    second = _checkout(client, token, scope="mine", method="card")
    assert second.status_code == 200, second.text
    assert second.json()["check_id"] == check_id_1  # same check, not a new one

    base_order_id = first.json()["base_order_id"]
    checks = _checks(org_a["org_id"], base_order_id)
    assert len(checks) == 1

    alerts = _alerts(org_a["org_id"], org_a["bot_number"])
    bill_alerts = [a for a in alerts if a["alert_type"] == "bill"]
    assert len(bill_alerts) == 1  # never a second waiter alert either


def test_checkout_empty_balance_is_clean_4xx_never_zero_value_check(client, org_a):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    # No order sent at all — nothing to charge.
    resp = _checkout(client, token, scope="mine", method="card")
    assert resp.status_code == 422
    assert resp.status_code < 500


# ── 4. Concurrent double checkout → ONE check, ONE alert ────────────────────
#
# A genuine two-OS-thread race through TestClient is NOT a valid way to test
# this in this codebase: app.services.database._pool is a single process-
# global asyncpg.Pool, and the established test pattern (see
# tests/test_diner_order_flow.py, tests/conftest.py::_reset_real_db_pool)
# requires resetting it BETWEEN calls precisely because concurrent access to
# it from two event loops corrupts the connection ("cannot perform operation:
# another operation is in progress") — a test-harness artifact, not a
# real-app bug (memory/e2e-shared-pool-flake.md documents the same root
# cause elsewhere). So the concurrency invariant is proven at the actual
# enforcement point instead: state_store.table_checkout_lock_acquire, the
# SAME primitive app/routes/diner.py::diner_checkout wraps its entire
# read-existing-checks/insert-new-check critical section in.

def test_table_checkout_lock_is_mutually_exclusive_and_ownership_safe():
    """Direct unit coverage of the primitive: while one holder has the lock,
    a second acquire attempt (any token) fails; only the ORIGINAL holder's
    token can release it; once released, a new acquire succeeds. This is
    exactly what makes two simultaneous diner_checkout calls on the same
    base_order_id serialize instead of racing."""
    from app.services import state_store

    async def _scenario():
        base_order_id = f"MESA-LOCKTEST-{uuid.uuid4().hex[:6]}"
        token_a = await state_store.table_checkout_lock_acquire(base_order_id)
        assert token_a is not None

        # A second concurrent request for the SAME table must be rejected
        # outright while the first is still in its critical section.
        token_b = await state_store.table_checkout_lock_acquire(base_order_id)
        assert token_b is None

        # Only the true holder's token can release it.
        await state_store.table_checkout_lock_release(base_order_id, "not-the-real-token")
        token_b_retry = await state_store.table_checkout_lock_acquire(base_order_id)
        assert token_b_retry is None  # still held — bogus release must be a no-op

        await state_store.table_checkout_lock_release(base_order_id, token_a)
        token_c = await state_store.table_checkout_lock_acquire(base_order_id)
        assert token_c is not None  # now free
        await state_store.table_checkout_lock_release(base_order_id, token_c)

    _run(_scenario())


def test_diner_checkout_endpoint_rejects_while_table_lock_held_by_another_request(client, org_a):
    """HTTP-level proof that diner_checkout actually USES the lock above:
    hold it externally (simulating an in-flight concurrent request on the
    SAME table), then hit the endpoint — must get 409, and must NOT create
    a check or an alert. Release the lock and confirm the retry succeeds
    with exactly one check/alert — never two."""
    from app.services import state_store

    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)

    base_order_id = _base_order_id(org_a["org_id"], org_a["table_id"])

    async def _hold_lock():
        return await state_store.table_checkout_lock_acquire(base_order_id)

    lock_token = _run(_hold_lock())
    assert lock_token is not None

    blocked = _checkout(client, token, scope="mine", method="card")
    assert blocked.status_code == 409

    async def _release():
        await state_store.table_checkout_lock_release(base_order_id, lock_token)

    _run(_release())

    ok = _checkout(client, token, scope="mine", method="card")
    assert ok.status_code == 200, ok.text

    checks = _checks(org_a["org_id"], base_order_id)
    assert len(checks) == 1  # the blocked attempt created nothing

    alerts = _alerts(org_a["org_id"], org_a["bot_number"])
    bill_alerts = [a for a in alerts if a["alert_type"] == "bill"]
    assert len(bill_alerts) == 1


# ── 5. Tip over 50% rejected ────────────────────────────────────────────────

def test_checkout_tip_over_50_percent_rejected(client, org_a):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)  # 28000
    _send(client, token)

    resp = _checkout(client, token, scope="mine", method="cash", tip_amount=15000)  # > 50% of 28000
    assert resp.status_code == 400
    assert "propina" in resp.json()["detail"].lower()

    # No check should have been created by the rejected attempt.
    base_order_id = _base_order_id(org_a["org_id"], org_a["table_id"])
    checks = _checks(org_a["org_id"], base_order_id)
    assert checks == []


def test_checkout_tip_within_50_percent_accepted(client, org_a):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)  # 28000
    _send(client, token)

    resp = _checkout(client, token, scope="mine", method="cash", tip_amount=14000)  # exactly 50%
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tip_amount"] == 14000
    assert data["total"] == 42000


# ── 6. Never trust a client-sent amount ─────────────────────────────────────

def test_checkout_ignores_any_client_sent_amount_field(client, org_a):
    """The request model has no `amount`/`total` field at all — the server
    computes everything from real table_orders rows. A client attempting to
    smuggle one in must be silently ignored (extra JSON keys), not honoured."""
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)  # 28000
    _send(client, token)

    resp = _post(client, "/api/diner/checkout", json={
        "token": token, "scope": "mine", "method": "card", "tip_amount": 0,
        "amount": 1, "total": 1,  # attempted smuggling
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["subtotal"] == 28000  # real total, not the smuggled "1"


# ── 7. Proposal visible to caja for the right org, invisible for another ───

def test_proposal_appears_in_checkout_proposals_for_right_org_only(client, org_a, org_b):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)
    resp = _checkout(client, token, scope="mine", method="card")
    assert resp.status_code == 200, resp.text
    base_order_id = resp.json()["base_order_id"]

    proposals_a = _proposals(org_a["org_id"])
    assert any(p["base_order_id"] == base_order_id for p in proposals_a)

    proposals_b = _proposals(org_b["org_id"])
    assert proposals_b == []  # never leaks cross-org


# ── 8. Paying through the EXISTING pay_check marks it invoiced + diner sees paid ──

def test_pay_check_marks_invoiced_and_diner_status_flips_to_paid(client, org_a, monkeypatch):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)  # 28000
    _send(client, token)

    resp = _checkout(client, token, scope="mine", method="cash")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    base_order_id = data["base_order_id"]
    check_id = data["check_id"]

    # Before payment: diner sees pending_waiter.
    status_before = _status(client, token)
    assert status_before.status_code == 200
    assert status_before.json()["checkout"]["status"] == "pending_waiter"

    _mock_pay_auth(monkeypatch, org_a["org_id"], org_a["bot_number"])
    monkeypatch.setattr("app.routes.tables.send_wa_interactive_nps", AsyncMock())
    pay_resp = _pay_check(client, base_order_id, check_id, amount=28000)
    assert pay_resp.status_code == 200, pay_resp.text

    status_after = _status(client, token)
    assert status_after.status_code == 200
    assert status_after.json()["checkout"]["status"] == "paid"


# ── 9. NPS: web identity never gets a WhatsApp push ─────────────────────────

def test_pay_check_web_identity_never_sends_whatsapp_nps(client, org_a, monkeypatch):
    """The bug this wave fixes: _farewell_and_nps used to call
    send_wa_interactive_nps for ANY phone, including a diner-web
    "web:<uuid4>" identity — a guaranteed-failing Meta API call on every
    single web table payment. Assert the send function is never invoked,
    while the (phone-agnostic) trigger_nps state machine still runs so the
    web page can render the survey."""
    session = _open(client, org_a["table_id"])
    token = session["token"]
    assert token.startswith("web:")
    _add(client, token, "bandeja", qty=1)
    _send(client, token)

    resp = _checkout(client, token, scope="mine", method="cash")
    assert resp.status_code == 200, resp.text
    base_order_id = resp.json()["base_order_id"]
    check_id = resp.json()["check_id"]

    _mock_pay_auth(monkeypatch, org_a["org_id"], org_a["bot_number"])
    wa_mock = AsyncMock()
    monkeypatch.setattr("app.routes.tables.send_wa_interactive_nps", wa_mock)

    pay_resp = _pay_check(client, base_order_id, check_id, amount=28000)
    assert pay_resp.status_code == 200, pay_resp.text

    wa_mock.assert_not_called()

    # NPS block should now be surfaced to the diner via GET /api/diner/status.
    status_resp = _status(client, token)
    assert status_resp.status_code == 200
    nps = status_resp.json()["nps"]
    assert nps is not None
    assert nps["stage"] == "score"
    assert nps["scale"] == 5


def test_nps_score_5_stores_without_comment(client, org_a, monkeypatch):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)
    resp = _checkout(client, token, scope="mine", method="cash")
    base_order_id = resp.json()["base_order_id"]
    check_id = resp.json()["check_id"]

    _mock_pay_auth(monkeypatch, org_a["org_id"], org_a["bot_number"])
    monkeypatch.setattr("app.routes.tables.send_wa_interactive_nps", AsyncMock())
    _pay_check(client, base_order_id, check_id, amount=28000)

    chat_resp = _post(client, "/api/diner/chat", json={"token": token, "message": "5"})
    assert chat_resp.status_code == 200, chat_resp.text

    async def _row():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await conn.fetchrow(
                "SELECT score, comment FROM nps_responses WHERE phone=$1 ORDER BY created_at DESC LIMIT 1",
                token,
            )
        finally:
            await conn.close()

    row = _run(_row())
    assert row is not None
    assert row["score"] == 5
    assert row["comment"] == ""

    # Survey closed — status endpoint no longer offers the block.
    status_resp = _status(client, token)
    assert status_resp.json()["nps"] is None


def test_nps_score_2_asks_for_comment_then_stores_it(client, org_a, monkeypatch):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)
    resp = _checkout(client, token, scope="mine", method="cash")
    base_order_id = resp.json()["base_order_id"]
    check_id = resp.json()["check_id"]

    _mock_pay_auth(monkeypatch, org_a["org_id"], org_a["bot_number"])
    monkeypatch.setattr("app.routes.tables.send_wa_interactive_nps", AsyncMock())
    _pay_check(client, base_order_id, check_id, amount=28000)

    score_resp = _post(client, "/api/diner/chat", json={"token": token, "message": "2"})
    assert score_resp.status_code == 200
    assert "mejorar" in score_resp.json()["message"].lower() or "comentario" in score_resp.json()["message"].lower()

    status_mid = _status(client, token)
    nps_mid = status_mid.json()["nps"]
    assert nps_mid is not None
    assert nps_mid["stage"] == "comment"

    comment_resp = _post(client, "/api/diner/chat", json={"token": token, "message": "El servicio fue lento"})
    assert comment_resp.status_code == 200

    async def _row():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            return await conn.fetchrow(
                "SELECT score, comment FROM nps_responses WHERE phone=$1 ORDER BY created_at DESC LIMIT 1",
                token,
            )
        finally:
            await conn.close()

    row = _run(_row())
    assert row["score"] == 2
    assert row["comment"] == "El servicio fue lento"

    assert _status(client, token).json()["nps"] is None


def test_nps_skip_closes_cleanly(client, org_a, monkeypatch):
    session = _open(client, org_a["table_id"])
    token = session["token"]
    _add(client, token, "bandeja", qty=1)
    _send(client, token)
    resp = _checkout(client, token, scope="mine", method="cash")
    base_order_id = resp.json()["base_order_id"]
    check_id = resp.json()["check_id"]

    _mock_pay_auth(monkeypatch, org_a["org_id"], org_a["bot_number"])
    monkeypatch.setattr("app.routes.tables.send_wa_interactive_nps", AsyncMock())
    _pay_check(client, base_order_id, check_id, amount=28000)

    skip_resp = _post(client, "/api/diner/chat", json={"token": token, "message": "no calificar"})
    assert skip_resp.status_code == 200

    assert _status(client, token).json()["nps"] is None

    async def _is_done():
        from app.services import state_store
        return await state_store.nps_is_done(token, org_a["bot_number"])

    assert _run(_is_done()) is True
