"""
tests/test_delivery_status_page.py
=====================================
Chunk 6 of the delivery/pickup web wave (docs/claude/delivery-web.md):
THE CUSTOMER STATUS PAGE `/pedido/{public_code}` — backend only.

Covers:
  A. app.services.delivery.compute_eta() — pure function, no DB (ETA
     computed in the SEDE'S OWN timezone, never UTC).
  B. GET /api/diner/order/{public_code} — the public read: right order,
     no customer phone/email, unknown code -> 404, rate limit trips.
  C. POST /api/diner/order/{public_code}/cancel — token ownership, illegal
     transitions, realtime publish.
  D. POST /api/diner/order/{public_code}/nps — eligibility, dedup, storage
     via the EXISTING nps_responses path, and the WhatsApp trap this chunk
     was told to avoid (send_wa_interactive_nps must never fire for a
     `web:` identity).
  E. The order-confirmation email — sent iff an email was given, never
     fails checkout.
  F. Realtime — db_get_order_ids_for_phone() scoping (the DB half of the
     diner stream's delivery-topic filter; the pure filter-function half is
     tested in tests/test_realtime.py), and every cashier/courier
     transition (including this chunk's own cancel) publishing the new
     "delivery_order.updated" topic alongside the existing "order.updated".

Fixture/seed style mirrors tests/test_delivery_cashier.py (same repo, same
conventions) — several helpers are imported from there rather than
duplicated.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone as dt_timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)

from tests.test_delivery_cashier import (  # noqa: E402 — needs the skipif above first
    _run, _reset_pool, _get, _post, _auth, _recent_utc,
    _seed_org, _seed_location, _seed_staff, _create_staff_token,
    _seed_order, _fetch_order, _teardown_org,
    _seed_checkout_org, _seed_session, _seed_cart, _default_checkout_body,
)


# ── Extra seed helpers specific to this file ────────────────────────────────


async def _seed_status_location(org_id: int, *, timezone_name: str = "America/Bogota", phone: str = "3011234567") -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            "INSERT INTO locations (org_id, name, phone, timezone) VALUES ($1, $2, $3, $4) RETURNING id",
            org_id, f"Sede {uuid.uuid4().hex[:6]}", phone, timezone_name,
        )
    finally:
        await conn.close()


async def _fetch_nps_rows(org_id: int) -> list[dict]:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        rows = await conn.fetch("SELECT * FROM nps_responses WHERE org_id = $1", org_id)
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _teardown_nps(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM nps_responses WHERE org_id = $1", org_id)
    finally:
        await conn.close()


# ══════════════════════════════════════════════════════════════════════════
# A. compute_eta() — pure function, no DB
# ══════════════════════════════════════════════════════════════════════════


def test_compute_eta_none_when_not_accepted():
    from app.services.delivery import compute_eta
    assert compute_eta(None, None, {"timezone": "America/Bogota"}) is None
    assert compute_eta(datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc), None, {}) is None


def test_compute_eta_uses_the_sedes_own_timezone_not_utc():
    """The core requirement (docs/claude/delivery-web.md chunk 6): ETA is
    rendered in the SEDE's timezone. Asia/Tokyo (UTC+9) makes a UTC-vs-local
    bug impossible to miss — 12:30 UTC must render as 21:30, never 12:30."""
    from app.services.delivery import compute_eta

    accepted_at = datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
    eta = compute_eta(accepted_at, 30, {"timezone": "Asia/Tokyo"})
    assert eta is not None
    assert eta["local_label"] == "21:30"
    assert eta["timezone"] == "Asia/Tokyo"

    # A DIFFERENT sede's timezone for the SAME instant gives a DIFFERENT
    # label — proof this isn't accidentally UTC or the server's own tz.
    eta_bogota = compute_eta(accepted_at, 30, {"timezone": "America/Bogota"})
    assert eta_bogota["local_label"] == "07:30"
    assert eta_bogota["local_label"] != eta["local_label"]


def test_compute_eta_handles_naive_accepted_at_as_utc():
    from app.services.delivery import compute_eta
    naive = datetime(2026, 1, 1, 12, 0)  # no tzinfo — must be treated as UTC
    eta = compute_eta(naive, 15, {"timezone": "America/Bogota"})
    assert eta["local_label"] == "07:15"


# ══════════════════════════════════════════════════════════════════════════
# B. GET /api/diner/order/{public_code} — the public read
# ══════════════════════════════════════════════════════════════════════════


def test_public_read_returns_order_and_never_leaks_phone_or_email(client):
    org_id = _run(_seed_org("Status Read Org"))
    try:
        location_id = _run(_seed_status_location(org_id, timezone_name="Asia/Tokyo", phone="3019998888"))
        code = "PUB" + uuid.uuid4().hex[:3].upper()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="en_preparacion",
            public_code=code, customer_name="Ana Pérez", customer_phone="3001112233",
            customer_email="ana@example.com", address="Calle 10 # 5-20",
            # accepted_at is TIMESTAMPTZ (migration 0082) — an aware datetime
            # is required. A NAIVE one would be interpreted in the DB
            # session's own TimeZone GUC on INSERT (not UTC), which is
            # exactly the trap this test exists to prove compute_eta()
            # avoids for the READ side (real production code only ever
            # writes accepted_at via SQL NOW(), which asyncpg always decodes
            # back as aware UTC — never naive).
            accepted_at=datetime(2026, 1, 1, 12, 0, tzinfo=dt_timezone.utc), estimated_minutes=30,
        ))

        resp = _get(client, f"/api/diner/order/{code}")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["public_code"] == code
        assert body["status"] == "en_preparacion"
        assert body["order_type"] == "domicilio"
        assert body["address"] == "Calle 10 # 5-20"
        assert body["location_phone"] == "3019998888"
        assert body["eta"]["local_label"] == "21:30", "ETA must use the sede's own timezone (Asia/Tokyo)"
        assert body["can_cancel"] is False, "en_preparacion is past the cancel window"

        raw = json.dumps(body)
        assert "3001112233" not in raw, "customer_phone must never appear in the public response"
        assert "ana@example.com" not in raw, "customer_email must never appear in the public response"
        assert "Ana Pérez" not in raw, "customer_name must never appear in the public response (PII behind a guessable-enough code)"
        assert "customer_phone" not in body
        assert "customer_email" not in body
        assert "phone" not in body
        assert order_id not in raw, "no internal order id beyond the public_code itself"
    finally:
        _run(_teardown_org(org_id))


def test_public_read_pickup_order_has_no_address(client):
    org_id = _run(_seed_org("Status Pickup Org"))
    try:
        location_id = _run(_seed_location(org_id))
        code = "PIK" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(
            org_id=org_id, location_id=location_id, order_type="recoger",
            public_code=code, address="should never be shown for pickup",
        ))
        resp = _get(client, f"/api/diner/order/{code}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["address"] is None
    finally:
        _run(_teardown_org(org_id))


def test_public_read_unknown_code_404(client):
    resp = _get(client, "/api/diner/order/DOESNOTEXIST")
    assert resp.status_code == 404


def test_public_read_rate_limit_trips(client):
    org_id = _run(_seed_org("Status Rate Limit Org"))
    try:
        location_id = _run(_seed_location(org_id))
        code = "RLT" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, public_code=code))

        statuses = [_get(client, f"/api/diner/order/{code}").status_code for _ in range(35)]
        assert 200 in statuses, "at least the first requests must succeed"
        assert 429 in statuses, "the per-IP rate limit must actually trip under a burst"
    finally:
        _run(_teardown_org(org_id))


def test_public_read_can_cancel_true_only_while_pending(client):
    org_id = _run(_seed_org("Status CanCancel Org"))
    try:
        location_id = _run(_seed_location(org_id))
        code = "CAN" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, status="pendiente_aceptacion", public_code=code))
        resp = _get(client, f"/api/diner/order/{code}")
        assert resp.json()["can_cancel"] is True
    finally:
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# C. POST /api/diner/order/{public_code}/cancel
# ══════════════════════════════════════════════════════════════════════════


def test_cancel_succeeds_with_the_creating_token(client):
    org_id = _run(_seed_org("Status Cancel Happy Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "CNA" + uuid.uuid4().hex[:3].upper()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="pendiente_aceptacion",
            public_code=code, phone=token,
        ))

        resp = _post(client, f"/api/diner/order/{code}/cancel", json={"token": token})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelado"

        row = _run(_fetch_order(order_id))
        assert row["status"] == "cancelado"
        assert row["cancelled_at"] is not None
        assert _recent_utc(row["cancelled_at"])
    finally:
        _run(_teardown_org(org_id))


def test_cancel_refused_with_another_token(client):
    org_id = _run(_seed_org("Status Cancel WrongToken Org"))
    try:
        location_id = _run(_seed_location(org_id))
        owner_token = f"web:{uuid.uuid4()}"
        code = "CNB" + uuid.uuid4().hex[:3].upper()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="pendiente_aceptacion",
            public_code=code, phone=owner_token,
        ))

        resp = _post(client, f"/api/diner/order/{code}/cancel", json={"token": f"web:{uuid.uuid4()}"})
        assert resp.status_code == 403, resp.text

        resp_no_token = _post(client, f"/api/diner/order/{code}/cancel", json={})
        assert resp_no_token.status_code == 403

        row = _run(_fetch_order(order_id))
        assert row["status"] == "pendiente_aceptacion", "a wrong-token cancel must never touch the order"
    finally:
        _run(_teardown_org(org_id))


def test_cancel_refused_after_acceptance(client):
    org_id = _run(_seed_org("Status Cancel PostAccept Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "CNC" + uuid.uuid4().hex[:3].upper()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="en_preparacion",
            public_code=code, phone=token,
        ))

        resp = _post(client, f"/api/diner/order/{code}/cancel", json={"token": token})
        assert resp.status_code == 409, resp.text

        row = _run(_fetch_order(order_id))
        assert row["status"] == "en_preparacion", "an accepted order must never be cancelled by the customer"
    finally:
        _run(_teardown_org(org_id))


def test_cancel_publishes_both_realtime_topics():
    from app.services import realtime
    from app.services.tenant_context import tenant_scope

    org_id = _run(_seed_org("Status Cancel Realtime Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "CND" + uuid.uuid4().hex[:3].upper()
        order_id = _run(_seed_order(
            org_id=org_id, location_id=location_id, status="pendiente_aceptacion",
            public_code=code, phone=token,
        ))

        async def _cancel_and_observe():
            async with realtime.subscribe(org_id) as queue:
                # The route itself calls delivery_repo + realtime — invoke it
                # over real HTTP so the whole wiring (not just the repo) is
                # under test, same convention as test_delivery_cashier.py.
                import httpx
                from app.main import app
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                    resp = await ac.post(f"/api/diner/order/{code}/cancel", json={"token": token})
                assert resp.status_code == 200, resp.text
                first = await asyncio.wait_for(queue.get(), timeout=2)
                second = await asyncio.wait_for(queue.get(), timeout=2)
                return {first["topic"], second["topic"]}

        topics = _run(_cancel_and_observe())
        assert topics == {"order.updated", "delivery_order.updated"}
    finally:
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# D. POST /api/diner/order/{public_code}/nps
# ══════════════════════════════════════════════════════════════════════════


def test_nps_refused_before_delivered(client):
    org_id = _run(_seed_org("Status Nps TooEarly Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "NPA" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, status="en_camino", public_code=code, phone=token))

        resp = _post(client, f"/api/diner/order/{code}/nps", json={"token": token, "score": 5})
        assert resp.status_code == 422, resp.text
    finally:
        _run(_teardown_org(org_id))


def test_nps_accepted_once_and_stored_via_existing_path(client):
    org_id = _run(_seed_org("Status Nps Happy Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        bot_number = "573000000999"
        code = "NPB" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(
            org_id=org_id, location_id=location_id, status="entregado",
            public_code=code, phone=token, bot_number=bot_number,
        ))

        resp = _post(client, f"/api/diner/order/{code}/nps", json={"token": token, "score": 5, "comment": "Excelente"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": False}

        rows = _run(_fetch_nps_rows(org_id))
        assert len(rows) == 1, "must write through the SAME nps_responses table the WhatsApp flow uses"
        assert rows[0]["phone"] == token
        assert rows[0]["bot_number"] == bot_number
        assert rows[0]["score"] == 5
        assert rows[0]["comment"] == "Excelente"
        # A delivery order has no table session; the rating must still count
        # for the sede that served it, or per-sede NPS silently loses it.
        assert rows[0]["location_id"] == location_id
        assert rows[0]["branch_id"] == location_id

        # Second submit — refused, and no second row.
        resp2 = _post(client, f"/api/diner/order/{code}/nps", json={"token": token, "score": 1})
        assert resp2.status_code == 409, resp2.text
        rows_after = _run(_fetch_nps_rows(org_id))
        assert len(rows_after) == 1, "a second submit must never write a second row"

        # The public read now reports it as already submitted.
        read_resp = _get(client, f"/api/diner/order/{code}")
        assert read_resp.json()["nps"]["already_submitted"] is True
    finally:
        _run(_teardown_nps(org_id))
        _run(_teardown_org(org_id))


def test_returning_customer_can_rate_each_of_their_orders(client):
    """The ordering page keeps the same session token across orders, so a
    per-token "already rated" flag let a returning customer rate only their
    FIRST order, ever. The guard is per order."""
    org_id = _run(_seed_org("Status Nps Repeat Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        first = "NPR" + uuid.uuid4().hex[:3].upper()
        second = "NPS" + uuid.uuid4().hex[:3].upper()
        for code in (first, second):
            _run(_seed_order(
                org_id=org_id, location_id=location_id, status="entregado",
                public_code=code, phone=token,
            ))

        r1 = _post(client, f"/api/diner/order/{first}/nps", json={"token": token, "score": 4})
        assert r1.status_code == 200, r1.text
        assert _get(client, f"/api/diner/order/{second}").json()["nps"]["already_submitted"] is False
        r2 = _post(client, f"/api/diner/order/{second}/nps", json={"token": token, "score": 2, "comment": "Tarde"})
        assert r2.status_code == 200, r2.text

        scores = sorted(r["score"] for r in _run(_fetch_nps_rows(org_id)))
        assert scores == [2, 4]
    finally:
        _run(_teardown_nps(org_id))
        _run(_teardown_org(org_id))


def test_nps_skip_marks_done_without_writing_a_score_row(client):
    org_id = _run(_seed_org("Status Nps Skip Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "NPC" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, status="entregado", public_code=code, phone=token))

        resp = _post(client, f"/api/diner/order/{code}/nps", json={"token": token, "skip": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["skipped"] is True

        rows = _run(_fetch_nps_rows(org_id))
        assert rows == [], "skipping before ever giving a score must not write a row"

        read_resp = _get(client, f"/api/diner/order/{code}")
        assert read_resp.json()["nps"]["already_submitted"] is True, "skip must still count as done (never asked again)"
    finally:
        _run(_teardown_nps(org_id))
        _run(_teardown_org(org_id))


def test_nps_refused_with_wrong_token(client):
    org_id = _run(_seed_org("Status Nps WrongToken Org"))
    try:
        location_id = _run(_seed_location(org_id))
        owner_token = f"web:{uuid.uuid4()}"
        code = "NPD" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, status="entregado", public_code=code, phone=owner_token))

        resp = _post(client, f"/api/diner/order/{code}/nps", json={"token": f"web:{uuid.uuid4()}", "score": 5})
        assert resp.status_code == 403, resp.text

        rows = _run(_fetch_nps_rows(org_id))
        assert rows == []
    finally:
        _run(_teardown_nps(org_id))
        _run(_teardown_org(org_id))


def test_nps_submit_never_sends_whatsapp(client):
    """The trap this chunk was told to avoid (docs/claude/delivery-web.md
    chunk 6): the WhatsApp-era NPS trigger (_farewell_and_nps ->
    send_wa_interactive_nps) fires for ANY phone, which would call Meta with
    an invalid number for a `web:` identity. Mocks ONLY the outbound
    WhatsApp client functions (never a repository), calls the real endpoint
    against the real DB, and asserts neither was ever invoked."""
    org_id = _run(_seed_org("Status Nps NoWhatsapp Org"))
    try:
        location_id = _run(_seed_location(org_id))
        token = f"web:{uuid.uuid4()}"
        code = "NPE" + uuid.uuid4().hex[:3].upper()
        _run(_seed_order(org_id=org_id, location_id=location_id, status="entregado", public_code=code, phone=token))

        with patch("app.routes.tables.send_wa_interactive_nps", new=AsyncMock()) as mock_interactive, \
             patch("app.routes.tables.send_wa_msg", new=AsyncMock()) as mock_msg:
            resp = _post(client, f"/api/diner/order/{code}/nps", json={"token": token, "score": 2, "comment": "Tardó mucho"})
            assert resp.status_code == 200, resp.text

        mock_interactive.assert_not_called()
        mock_msg.assert_not_called()

        rows = _run(_fetch_nps_rows(org_id))
        assert len(rows) == 1 and rows[0]["score"] == 2
    finally:
        _run(_teardown_nps(org_id))
        _run(_teardown_org(org_id))


# ══════════════════════════════════════════════════════════════════════════
# E. Order-confirmation email
# ══════════════════════════════════════════════════════════════════════════


def test_checkout_sends_confirmation_email_with_the_right_link_when_email_given(client):
    info = _run(_seed_checkout_org())
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 40000.0, "line_id": "a1"},
        ]))

        with patch("app.services.email.send_email", new=AsyncMock(return_value=True)) as mock_send:
            resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
                token, customer_email="ana@example.com",
            ))
            assert resp.status_code == 200, resp.text
            public_code = resp.json()["public_code"]
            time.sleep(0.3)  # let the fire-and-forget background task run

        mock_send.assert_called_once()
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "ana@example.com"
        assert f"/pedido/{public_code}" in kwargs["html"]
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_does_not_send_email_when_none_given(client):
    info = _run(_seed_checkout_org())
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 40000.0, "line_id": "a1"},
        ]))

        with patch("app.services.email.send_email", new=AsyncMock(return_value=True)) as mock_send:
            resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
            assert resp.status_code == 200, resp.text
            time.sleep(0.3)

        mock_send.assert_not_called()
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_succeeds_even_when_email_backend_fails(client):
    info = _run(_seed_checkout_org())
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"]))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 40000.0, "line_id": "a1"},
        ]))

        with patch("app.services.email.send_email", new=AsyncMock(side_effect=RuntimeError("smtp is down"))):
            resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
                token, customer_email="ana@example.com",
            ))
            assert resp.status_code == 200, resp.text
            time.sleep(0.3)  # give the background task time to hit (and swallow) the error
    finally:
        _run(_teardown_org(info["org_id"]))


# ══════════════════════════════════════════════════════════════════════════
# F. Realtime — db_get_order_ids_for_phone() scoping + transitions publish
# ══════════════════════════════════════════════════════════════════════════


def test_db_get_order_ids_for_phone_scopes_to_owner_and_org():
    """The DB half of the diner stream's delivery-topic filter
    (app/routes/diner.py::_make_diner_filter — the pure filter-function half
    is unit-tested in tests/test_realtime.py). Combines the REAL query with
    the REAL filter to prove a delivery session receives its own order's
    event and NOT another customer's, even within the SAME org."""
    from app.repositories import delivery_repo
    from app.routes.diner import _make_diner_filter
    from app.services.tenant_context import tenant_scope

    org_id = _run(_seed_org("Status Realtime Scope Org"))
    try:
        location_id = _run(_seed_location(org_id))
        my_token = f"web:{uuid.uuid4()}"
        other_token = f"web:{uuid.uuid4()}"
        my_order_id = _run(_seed_order(org_id=org_id, location_id=location_id, phone=my_token))
        other_order_id = _run(_seed_order(org_id=org_id, location_id=location_id, phone=other_token))

        async def _query():
            with tenant_scope(org_id):
                return await delivery_repo.db_get_order_ids_for_phone(org_id, my_token)

        owned_ids = _run(_query())
        assert owned_ids == [my_order_id]
        assert other_order_id not in owned_ids

        filt = _make_diner_filter(None, frozenset(owned_ids))
        assert filt({"topic": "delivery_order.updated", "entity_id": my_order_id}) is True
        assert filt({"topic": "delivery_order.updated", "entity_id": other_order_id}) is False
    finally:
        _run(_teardown_org(org_id))


def test_every_cashier_and_courier_transition_publishes_delivery_topic():
    """Every status transition (accept, reject, assign-courier, en-route,
    delivered) must publish "delivery_order.updated" alongside the existing
    staff-only "order.updated" (app.services.realtime.publish_delivery_status).

    Calls the REAL route handler functions directly (real delivery_repo
    writes, real realtime.publish) rather than through TestClient/ASGI: the
    HTTP/auth wiring for these endpoints is already covered end-to-end in
    tests/test_delivery_cashier.py, and running several sequential ASGI
    calls back-to-back against the shared app pool inside one manually
    driven event loop hits the SAME known asyncpg/BaseHTTPMiddleware
    connection-reuse race documented in the "E2E shared-pool flake" memory
    note — orthogonal to what this test needs to prove."""
    from app.services import realtime
    from app.routes import staff_delivery
    from app.services.tenant_context import tenant_scope

    org_id = _run(_seed_org("Status Transitions Realtime Org"))
    try:
        location_id = _run(_seed_location(org_id))
        cashier_id = _run(_seed_staff(org_id, location_id, role="caja"))
        courier_id = _run(_seed_staff(org_id, location_id, role="domiciliario"))
        # Chunk 7 (docs/claude/delivery-web.md) added is_admin/is_cashier/
        # is_courier to the scope dict delivery_scope() resolves — the
        # per-endpoint role gates (_require_cashier_or_admin,
        # _require_can_transition) read them directly. This hand-built scope
        # bypasses that dependency (see the docstring above for why), so it
        # must mirror a CASHIER's resolved scope by hand.
        scope = {
            "user": {}, "org_id": org_id, "location_id": location_id, "staff_id": cashier_id,
            "is_admin": False, "is_cashier": True, "is_courier": False,
        }

        async def _drain_topics(queue, n=2):
            topics = set()
            for _ in range(n):
                event = await asyncio.wait_for(queue.get(), timeout=2)
                topics.add(event["topic"])
            return topics

        async def _scenario():
            results = {}

            order_1 = await _seed_order(org_id=org_id, location_id=location_id)
            async with realtime.subscribe(org_id) as queue:
                with tenant_scope(org_id):
                    await staff_delivery.accept_delivery_order(
                        order_1, staff_delivery.AcceptOrderRequest(eta_minutes=15), scope,
                    )
                results["accept"] = await _drain_topics(queue)

            order_2 = await _seed_order(org_id=org_id, location_id=location_id)
            async with realtime.subscribe(org_id) as queue:
                with tenant_scope(org_id):
                    await staff_delivery.reject_delivery_order(
                        order_2, staff_delivery.RejectOrderRequest(reason="sin stock"), scope,
                    )
                results["reject"] = await _drain_topics(queue)

            order_3 = await _seed_order(org_id=org_id, location_id=location_id)
            with tenant_scope(org_id):
                await staff_delivery.accept_delivery_order(
                    order_3, staff_delivery.AcceptOrderRequest(eta_minutes=10), scope,
                )

            async with realtime.subscribe(org_id) as queue:
                with tenant_scope(org_id):
                    await staff_delivery.assign_courier(
                        order_3, staff_delivery.AssignCourierRequest(courier_staff_id=courier_id), scope,
                    )
                results["assign_courier"] = await _drain_topics(queue)

            async with realtime.subscribe(org_id) as queue:
                with tenant_scope(org_id):
                    await staff_delivery.mark_en_route(order_3, scope)
                results["en_route"] = await _drain_topics(queue)

            async with realtime.subscribe(org_id) as queue:
                with tenant_scope(org_id):
                    await staff_delivery.mark_delivered(order_3, scope)
                results["delivered"] = await _drain_topics(queue)

            return results

        results = _run(_scenario())
        for name, topics in results.items():
            assert topics == {"order.updated", "delivery_order.updated"}, f"{name} must publish both topics, got {topics}"
    finally:
        _run(_teardown_org(org_id))
