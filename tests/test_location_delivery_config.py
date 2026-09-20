"""
tests/test_location_delivery_config.py
========================================
Chunk 8 of the delivery/pickup web wave (docs/claude/delivery-web.md):
THE RESTAURANT'S CONFIGURATION SIDE, part A — the owner/admin-only
GET/PUT /api/locations/{id}/delivery-config endpoints
(app/routes/location_delivery.py).

Covers:
  - writes and reads back the exact values (money, radius, prep minutes,
    payment methods, phone);
  - rejects negative money, a bad radius, and unknown payment methods;
  - refuses a sede belonging to another org (deliberately colliding ids)
    and refuses a non-admin caller;
  - the actual checkout (POST /api/diner/delivery/checkout) honors a newly
    set minimum order, delivery fee and payment-method allowlist, end to
    end — not just the config endpoint's own echo.

Real database, real HTTP (TestClient), following the seed/teardown +
_run/_get/_post/_put event-loop convention already used by
tests/test_delivery_cashier.py and tests/test_waiter_alerts_location.py in
this same repo.
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


# ── Event-loop / HTTP helpers (mirrors test_delivery_cashier.py) ───────────


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


def _put(client, url, **kwargs):
    _reset_pool()
    return client.put(url, **kwargs)


def _post(client, url, **kwargs):
    _reset_pool()
    return client.post(url, **kwargs)


_AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _auth_as(monkeypatch, org_id: int, role: str = "owner", username: str = "loc_cfg_owner",
             location_id: int | None = None):
    """Make get_current_user resolve to a real org_id with the given role,
    without a `staff` row — mirrors tests/test_waiter_alerts_location.py's
    _auth_as_location, but for a plain admin-dashboard `users` row.

    location_id defaults to None: owner/admin manage every sede from this
    surface and are not bound to one. A `gerente` IS bound to theirs, so
    those tests pass it explicitly."""
    from app.services import database as db

    async def _verify_token(token):
        return username

    async def _get_user(uname):
        if uname != username:
            return None
        return {
            "username": username, "branch_id": None,
            "org_id": org_id, "location_id": location_id,
            "role": role, "restaurant_name": "",
        }

    monkeypatch.setattr("app.routes.deps.verify_token", _verify_token)
    monkeypatch.setattr(db, "db_get_user", _get_user)


# ── Seed helpers ─────────────────────────────────────────────────────────


async def _advance_location_seq(conn, forced_id: int) -> None:
    """A forced explicit id does NOT advance locations' BIGSERIAL sequence —
    see tests/test_delivery_repo.py's helper of the same name."""
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        forced_id,
    )


async def _seed_org(name: str, features: dict | None = None) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:8]
        return await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            name, f"{name.lower().replace(' ', '-')}-{suffix}", json.dumps(features or {}),
        )
    finally:
        await conn.close()


async def _fetch_slug(org_id: int) -> str:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


async def _seed_location(
    org_id: int, name: str = "Sede", loc_id: int | None = None,
    delivery_config: dict | None = None, phone: str | None = None,
) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        if loc_id is not None:
            new_id = await conn.fetchval(
                """INSERT INTO locations (id, org_id, name, delivery_config, phone)
                   VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING id""",
                loc_id, org_id, name, json.dumps(delivery_config or {}), phone,
            )
            await _advance_location_seq(conn, loc_id)
            return new_id
        return await conn.fetchval(
            """INSERT INTO locations (org_id, name, delivery_config, phone)
               VALUES ($1, $2, $3::jsonb, $4) RETURNING id""",
            org_id, name, json.dumps(delivery_config or {}), phone,
        )
    finally:
        await conn.close()


async def _fetch_location_config(location_id: int) -> dict:
    """A raw asyncpg connection (no jsonb codec registered — only the app's
    own pool has that) reads delivery_config back as a JSON string; parse it
    here so callers can compare against plain dicts."""
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow(
            "SELECT delivery_config, phone FROM locations WHERE id = $1", location_id,
        )
        if not row:
            return {}
        d = dict(row)
        if isinstance(d.get("delivery_config"), str):
            d["delivery_config"] = json.loads(d["delivery_config"])
        return d
    finally:
        await conn.close()


async def _seed_session(org_id: int, location_id: int, bot_number: str, order_mode: str = "delivery") -> str:
    token = f"web:{uuid.uuid4()}"
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO diner_sessions (token, org_id, location_id, table_id, table_name, bot_number, order_mode)
               VALUES ($1, $2, $3, NULL, NULL, $4, $5)""",
            token, org_id, location_id, bot_number, order_mode,
        )
    finally:
        await conn.close()
    return token


async def _seed_cart(token: str, bot_number: str, org_id: int, items: list) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO carts (phone, bot_number, cart_data, updated_at, org_id)
               VALUES ($1, $2, $3::jsonb, NOW(), $4)
               ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data = EXCLUDED.cart_data""",
            token, bot_number, json.dumps({"items": items, "order_type": None, "address": None, "notes": ""}),
            org_id,
        )
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("DELETE FROM orders WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM carts WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def org_with_location():
    org_id = _run(_seed_org("LocCfg Org"))
    location_id = _run(_seed_location(org_id, name="Sede Centro"))
    try:
        yield {"org_id": org_id, "location_id": location_id}
    finally:
        _run(_teardown_org(org_id))


def _valid_payload(**overrides) -> dict:
    body = {
        "delivery_enabled": True,
        "pickup_enabled": True,
        "delivery_fee": 5000,
        "min_order": 20000,
        "radius_km": 8.5,
        "prep_minutes": 25,
        "payment_methods": ["efectivo", "nequi"],
        "phone": "3011234567",
    }
    body.update(overrides)
    return body


# ── Write + read back exact values ──────────────────────────────────────


def test_put_writes_and_get_reads_back_exact_values(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)

    payload = _valid_payload()
    resp = _put(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS, json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    eff = data["effective"]
    assert eff["delivery_enabled"] is True
    assert eff["pickup_enabled"] is True
    assert eff["delivery_fee"] == 5000
    assert eff["min_order"] == 20000
    assert eff["radius_km"] == 8.5
    assert eff["prep_minutes"] == 25
    assert sorted(eff["payment_methods"]) == ["efectivo", "nequi"]
    assert data["phone"] == "3011234567"
    assert data["public_link"].startswith("/pedir/")
    # Every field was explicitly set on THIS sede — none of it is inherited.
    assert all(data["overrides"].values())

    # Read it back through GET too, independently of the PUT's own response.
    resp2 = _get(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS)
    assert resp2.status_code == 200, resp2.text
    data2 = resp2.json()
    assert data2["effective"] == eff
    assert data2["phone"] == "3011234567"

    # And directly in the DB — the config endpoint isn't just echoing input.
    row = _run(_fetch_location_config(location_id))
    stored_cfg = row["delivery_config"]
    assert isinstance(stored_cfg, dict)
    assert stored_cfg["delivery_fee"] == 5000
    assert stored_cfg["min_order"] == 20000
    assert sorted(stored_cfg["payment_methods"]) == ["efectivo", "nequi"]
    assert row["phone"] == "3011234567"


def test_inherited_fields_are_flagged_when_not_overridden(client, monkeypatch):
    org_id = _run(_seed_org("LocCfg Inherit Org", features={
        "delivery_fee": 3000, "min_order": 10000, "delivery_radius_km": 6,
        "payment_methods": ["efectivo"],
    }))
    location_id = _run(_seed_location(org_id, name="Sede Sin Config"))
    try:
        _auth_as(monkeypatch, org_id)
        resp = _get(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Nothing was ever set on the sede itself -> every key is inherited.
        assert all(v is False for v in data["overrides"].values())
        assert data["effective"]["delivery_fee"] == 3000
        assert data["org_default"]["delivery_fee"] == 3000
    finally:
        _run(_teardown_org(org_id))


# ── Validation ────────────────────────────────────────────────────────────


def test_rejects_negative_delivery_fee(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(delivery_fee=-100),
    )
    assert resp.status_code == 400, resp.text


def test_rejects_negative_min_order(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(min_order=-1),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("bad_radius", [0, -5, 500])
def test_rejects_bad_radius(client, org_with_location, monkeypatch, bad_radius):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(radius_km=bad_radius),
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize("bad_prep", [0, -10, 1000])
def test_rejects_bad_prep_minutes(client, org_with_location, monkeypatch, bad_prep):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(prep_minutes=bad_prep),
    )
    assert resp.status_code == 400, resp.text


def test_rejects_unknown_payment_method(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(payment_methods=["paypal"]),
    )
    assert resp.status_code == 400, resp.text
    assert "paypal" in resp.json()["detail"]


def test_rejects_both_delivery_and_pickup_off(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id)
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(delivery_enabled=False, pickup_enabled=False),
    )
    assert resp.status_code == 400, resp.text

    # A rejected PUT must not have mutated the row.
    row = _run(_fetch_location_config(location_id))
    assert row["delivery_config"] == {}


# ── Authorization: foreign org + non-admin caller ──────────────────────────


def test_refuses_a_sede_of_another_org_with_colliding_ids(client, monkeypatch):
    """Deliberate id collision: org_b's own id equals a location that
    belongs to org_a — same shape as tests/test_delivery_repo.py's
    collision fixture."""
    org_a = _run(_seed_org("LocCfg Collision Org A"))
    org_b = _run(_seed_org("LocCfg Collision Org B"))
    loc_l = _run(_seed_location(org_a, name="A's sede (collides with org B id)", loc_id=org_b))
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
    try:
        _auth_as(monkeypatch, org_b, username="collision_owner")
        resp = _get(client, f"/api/locations/{loc_l}/delivery-config", headers=_AUTH_HEADERS)
        assert resp.status_code == 404, resp.text

        resp2 = _put(
            client, f"/api/locations/{loc_l}/delivery-config",
            headers=_AUTH_HEADERS, json=_valid_payload(),
        )
        assert resp2.status_code == 404, resp2.text

        # Org A's real config must be untouched by org B's attempt.
        row = _run(_fetch_location_config(loc_l))
        assert row["delivery_config"] == {}
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


@pytest.mark.parametrize("role", ["mesero", "caja", "cocina"])
def test_refuses_non_admin_caller(client, org_with_location, monkeypatch, role):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id, role=role, username=f"non_admin_{role}")
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(),
    )
    assert resp.status_code == 403, resp.text

    resp2 = _get(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS)
    assert resp2.status_code == 403, resp2.text


def test_gerente_may_configure_their_own_sede(client, org_with_location, monkeypatch):
    """PM decision 2026-09-20: whoever runs the sede decides whether it takes
    domicilios today, so a gerente reads AND writes their own sede's config."""
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id, role="gerente", username="gerente_own",
             location_id=location_id)

    resp = _put(client, f"/api/locations/{location_id}/delivery-config",
                headers=_AUTH_HEADERS, json=_valid_payload(delivery_fee=7000))
    assert resp.status_code == 200, resp.text

    read = _get(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS)
    assert read.status_code == 200, read.text
    assert float(read.json()["effective"]["delivery_fee"]) == 7000.0


def test_gerente_may_not_configure_another_sede(client, org_with_location, monkeypatch):
    """Same org, different sede — a gerente is scoped to exactly one."""
    org_id = org_with_location["org_id"]
    other_id = _run(_seed_location(org_id, name="Sede Norte"))
    mine = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id, role="gerente", username="gerente_other",
             location_id=mine)

    resp = _get(client, f"/api/locations/{other_id}/delivery-config", headers=_AUTH_HEADERS)
    assert resp.status_code == 403, resp.text

    resp2 = _put(client, f"/api/locations/{other_id}/delivery-config",
                 headers=_AUTH_HEADERS, json=_valid_payload())
    assert resp2.status_code == 403, resp2.text


def test_gerente_without_a_sede_is_refused(client, org_with_location, monkeypatch):
    """Never default to "some" sede — guessing between ids is the ambiguity
    rls-multitenant.md forbids."""
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id, role="gerente", username="gerente_nosede",
             location_id=None)

    resp = _get(client, f"/api/locations/{location_id}/delivery-config", headers=_AUTH_HEADERS)
    assert resp.status_code == 403, resp.text
    assert "sede" in resp.json()["detail"].lower()


def test_admin_role_is_allowed_not_only_owner(client, org_with_location, monkeypatch):
    org_id = org_with_location["org_id"]
    location_id = org_with_location["location_id"]
    _auth_as(monkeypatch, org_id, role="admin", username="admin_caller")
    resp = _put(
        client, f"/api/locations/{location_id}/delivery-config",
        headers=_AUTH_HEADERS, json=_valid_payload(),
    )
    assert resp.status_code == 200, resp.text


# ── End-to-end: checkout honors a newly-set config ─────────────────────────


def test_checkout_honors_newly_set_min_order_fee_and_payment_method(client, monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    bot_number = f"573{suffix[:9]}"
    org_id = _run(_seed_org(f"LocCfg Checkout Org {suffix}", features={"currency": "COP"}))
    location_id = _run(_seed_location(org_id, name=f"Sede {suffix}"))

    async def _finish_location_setup():
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            await conn.execute(
                "UPDATE locations SET whatsapp_number=$1, latitude=4.6097, longitude=-74.0817 WHERE id=$2",
                bot_number, location_id,
            )
        finally:
            await conn.close()
    _run(_finish_location_setup())

    try:
        # Set the config through the real endpoint under test — min_order
        # 50000, delivery_fee 7000, ONLY "nequi" accepted.
        _auth_as(monkeypatch, org_id, username="checkout_owner")
        cfg_resp = _put(
            client, f"/api/locations/{location_id}/delivery-config",
            headers=_AUTH_HEADERS,
            json=_valid_payload(min_order=50000, delivery_fee=7000, radius_km=50, payment_methods=["nequi"]),
        )
        assert cfg_resp.status_code == 200, cfg_resp.text

        # Below the new minimum -> refused, naming the reason.
        token = _run(_seed_session(org_id, location_id, bot_number))
        _run(_seed_cart(token, bot_number, org_id, [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 10000, "price": 10000},
        ]))
        low_body = {
            "token": token, "idempotency_key": uuid.uuid4().hex,
            "customer_name": "Ana Pérez", "customer_phone": "3001112233",
            "address": "Calle 10 # 5-20", "lat": 4.6097, "lon": -74.0817,
            "payment_method": "nequi", "tip_amount": 0.0,
        }
        resp_low = _post(client, "/api/diner/delivery/checkout", json=low_body)
        assert resp_low.status_code == 422, resp_low.text
        assert resp_low.json()["detail"]["reason"] == "below_minimum"

        # Above minimum but with a payment method the sede does NOT accept.
        token2 = _run(_seed_session(org_id, location_id, bot_number))
        _run(_seed_cart(token2, bot_number, org_id, [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 60000, "price": 60000},
        ]))
        wrong_method_body = dict(low_body, token=token2, idempotency_key=uuid.uuid4().hex, payment_method="efectivo")
        resp_wrong = _post(client, "/api/diner/delivery/checkout", json=wrong_method_body)
        assert resp_wrong.status_code == 422, resp_wrong.text
        assert resp_wrong.json()["detail"]["reason"] == "payment_method_not_allowed"

        # Above minimum, allowed method -> succeeds, and the fee from the
        # config we just set is the one actually charged.
        token3 = _run(_seed_session(org_id, location_id, bot_number))
        _run(_seed_cart(token3, bot_number, org_id, [
            {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 60000, "price": 60000},
        ]))
        ok_body = dict(low_body, token=token3, idempotency_key=uuid.uuid4().hex, payment_method="nequi")
        resp_ok = _post(client, "/api/diner/delivery/checkout", json=ok_body)
        assert resp_ok.status_code == 200, resp_ok.text
        data = resp_ok.json()
        assert data["subtotal"] == 60000
        assert data["delivery_fee"] == 7000
        assert data["total"] == 67000
        assert data["payment_method"] == "nequi"
    finally:
        _run(_teardown_org(org_id))
