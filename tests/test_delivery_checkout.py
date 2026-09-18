"""
tests/test_delivery_checkout.py
=================================
Chunk 3 of the delivery/pickup web wave (docs/claude/delivery-web.md):
THE CHECKOUT AND ORDER CREATION — backend only.

Three layers of test, matching how the code is actually split (same shape
as tests/test_delivery_entry.py):

  A. Pure-function tests for app.services.delivery.validate_coverage_and_hours()
     and validate_schedule() — no DB.

  B. Real-database repo-level tests for
     app.repositories.delivery_repo.db_create_delivery_order() and
     db_count_open_orders_for_phone(), using the mesio_app / _ConnProxy
     fixture (docs/claude/testing.md) so RLS is genuinely exercised.

  C. HTTP-level tests (TestClient against the real app) for
     POST /api/diner/delivery/checkout and POST /api/diner/delivery/payment-proof,
     following tests/test_delivery_entry.py's fixture style (superuser DB —
     the documented green convention).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import asyncpg
import pytest

from app.services.tenant_context import tenant_scope

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


# ── A. validate_coverage_and_hours() / validate_schedule() — pure, no DB ────

_OPEN_ALL_WEEK = {
    d: {"open": "00:00", "close": "23:59", "closed": False}
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}
_CLOSED_ALL_WEEK = {
    d: {"open": None, "close": None, "closed": True}
    for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def test_coverage_delivery_disabled_is_refused():
    from app.services.delivery import REASON_DELIVERY_DISABLED, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _OPEN_ALL_WEEK, "active": True}
    cfg = {"delivery_enabled": False, "pickup_enabled": True, "radius_km": Decimal("5")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="delivery", lat=4.6, lon=-74.0,
    )
    assert reason == REASON_DELIVERY_DISABLED


def test_coverage_no_gps_for_delivery_is_refused():
    from app.services.delivery import REASON_NO_GPS, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _OPEN_ALL_WEEK, "active": True}
    cfg = {"delivery_enabled": True, "pickup_enabled": True, "radius_km": Decimal("5")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="delivery", lat=None, lon=None,
    )
    assert reason == REASON_NO_GPS


def test_coverage_point_outside_radius_is_refused():
    from app.services.delivery import REASON_OUT_OF_COVERAGE, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _OPEN_ALL_WEEK, "active": True}
    cfg = {"delivery_enabled": True, "pickup_enabled": True, "radius_km": Decimal("1")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="delivery", lat=10.0, lon=-74.0,
    )
    assert reason == REASON_OUT_OF_COVERAGE


def test_coverage_pickup_disabled_is_a_distinct_reason():
    from app.services.delivery import REASON_PICKUP_DISABLED, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _OPEN_ALL_WEEK, "active": True}
    cfg = {"delivery_enabled": False, "pickup_enabled": False, "radius_km": Decimal("5")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="pickup", lat=None, lon=None,
    )
    assert reason == REASON_PICKUP_DISABLED


def test_coverage_closed_sede_is_refused_even_when_covering():
    from app.services.delivery import REASON_ALL_CLOSED, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _CLOSED_ALL_WEEK, "active": True}
    cfg = {"delivery_enabled": True, "pickup_enabled": True, "radius_km": Decimal("50")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="delivery", lat=4.6, lon=-74.0,
    )
    assert reason == REASON_ALL_CLOSED


def test_coverage_inactive_location_is_refused():
    from app.services.delivery import REASON_ALL_CLOSED, validate_coverage_and_hours

    location = {"latitude": 4.6, "longitude": -74.0, "opening_hours": _OPEN_ALL_WEEK, "active": False}
    cfg = {"delivery_enabled": True, "pickup_enabled": True, "radius_km": Decimal("50")}
    reason = validate_coverage_and_hours(
        location=location, config=cfg, order_mode="pickup", lat=None, lon=None,
    )
    assert reason == REASON_ALL_CLOSED


def test_schedule_next_day_is_refused():
    from app.services.delivery import REASON_SCHEDULE_NOT_TODAY, validate_schedule

    location = {"timezone": "America/Bogota", "opening_hours": _OPEN_ALL_WEEK}
    now = datetime(2026, 1, 6, 15, 0, tzinfo=dt_timezone.utc)  # 10:00 Bogota
    tomorrow = now + timedelta(days=1)
    reason = validate_schedule(location, tomorrow, now=now)
    assert reason == REASON_SCHEDULE_NOT_TODAY


def test_schedule_in_the_past_is_refused():
    from app.services.delivery import REASON_SCHEDULE_IN_PAST, validate_schedule

    location = {"timezone": "America/Bogota", "opening_hours": _OPEN_ALL_WEEK}
    now = datetime(2026, 1, 6, 15, 0, tzinfo=dt_timezone.utc)
    an_hour_ago = now - timedelta(hours=1)
    reason = validate_schedule(location, an_hour_ago, now=now)
    assert reason == REASON_SCHEDULE_IN_PAST


def test_schedule_outside_hours_same_day_is_refused():
    from app.services.delivery import REASON_SCHEDULE_OUTSIDE_HOURS, validate_schedule

    location = {"timezone": "America/Bogota", "opening_hours": _CLOSED_ALL_WEEK}
    now = datetime(2026, 1, 6, 15, 0, tzinfo=dt_timezone.utc)
    later_today = now + timedelta(hours=1)
    reason = validate_schedule(location, later_today, now=now)
    assert reason == REASON_SCHEDULE_OUTSIDE_HOURS


def test_schedule_same_day_within_hours_is_accepted():
    from app.services.delivery import validate_schedule

    location = {"timezone": "America/Bogota", "opening_hours": _OPEN_ALL_WEEK}
    now = datetime(2026, 1, 6, 15, 0, tzinfo=dt_timezone.utc)
    later_today = now + timedelta(hours=1)
    assert validate_schedule(location, later_today, now=now) is None


# ── image_host.upload_delivery_proof() — real magic-byte check, no network ──


def test_upload_delivery_proof_rejects_non_image_bytes(monkeypatch):
    from app.services import image_host

    monkeypatch.setattr(image_host, "_CLOUD_NAME", "dummy")
    monkeypatch.setattr(image_host, "_API_KEY", "dummy")
    monkeypatch.setattr(image_host, "_API_SECRET", "dummy")

    result = image_host.upload_delivery_proof(1, b"this is definitely not an image", "image/png")
    assert result == {"error": "not_an_image"}


def test_upload_delivery_proof_rejects_oversized_file(monkeypatch):
    from app.services import image_host

    monkeypatch.setattr(image_host, "_CLOUD_NAME", "dummy")
    monkeypatch.setattr(image_host, "_API_KEY", "dummy")
    monkeypatch.setattr(image_host, "_API_SECRET", "dummy")

    huge = b"\xff\xd8\xff" + b"0" * (image_host._MAX_PROOF_BYTES + 1)
    result = image_host.upload_delivery_proof(1, huge, "image/jpeg")
    assert result == {"error": "file_too_large"}


# ── asyncpg Connection.__slots__ workaround (docs/claude/testing.md) ────────


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


class _PoolShim:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)

    async def close(self):
        pass


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_):
        pass


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def db_conn(raw_pool, monkeypatch):
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
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


async def _set_scope(conn, org_id: int) -> None:
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org_id))


async def _seed_org(conn, name: str) -> int:
    return await conn.fetchval(
        "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
        name, f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}",
    )


async def _advance_location_seq(conn, forced_id: int) -> None:
    """See tests/test_delivery_repo.py's helper of the same name: a forced
    explicit id does NOT advance locations' BIGSERIAL sequence, which makes a
    collision test pass alone and fail in the full suite unless this runs."""
    await conn.execute(
        "SELECT setval("
        "  pg_get_serial_sequence('locations', 'id'),"
        "  GREATEST($1::bigint, (SELECT COALESCE(last_value, 1) FROM locations_id_seq))"
        ")",
        forced_id,
    )


async def _seed_location(conn, org_id: int, name: str = "Sede", loc_id: int | None = None) -> int:
    if loc_id is not None:
        new_id = await conn.fetchval(
            "INSERT INTO locations (id, org_id, name) VALUES ($1, $2, $3) RETURNING id",
            loc_id, org_id, name,
        )
        await _advance_location_seq(conn, loc_id)
        return new_id
    return await conn.fetchval(
        "INSERT INTO locations (org_id, name) VALUES ($1, $2) RETURNING id",
        org_id, name,
    )


_SAMPLE_ITEMS = [{"name": "Bandeja Paisa", "quantity": 2, "subtotal": 40000.0, "line_id": "a1"}]


# ── B. db_create_delivery_order() / db_count_open_orders_for_phone() ────────


async def test_create_delivery_order_writes_expected_fields(db_conn):
    from app.repositories.delivery_repo import STATUS_PENDING_ACCEPTANCE, db_create_delivery_order

    org_id = await _seed_org(db_conn, "Writer Org")
    location_id = await _seed_location(db_conn, org_id)

    with tenant_scope(org_id):
        created = await db_create_delivery_order(
            org_id=org_id, location_id=location_id, phone="web:abc123", bot_number="573000000000",
            order_type="domicilio", items=_SAMPLE_ITEMS, address="Calle 1 # 2-3",
            subtotal=Decimal("40000"), delivery_fee=Decimal("3000"), tip_amount=Decimal("2000"),
            total=Decimal("45000"), payment_method="efectivo", cash_change_for=Decimal("50000"),
            customer_name="Ana", customer_phone="3001234567", customer_email="ana@example.com",
            delivery_lat=4.6097, delivery_lon=-74.0817, proof_url=None, scheduled_pickup_at=None,
        )

    assert created["status"] == STATUS_PENDING_ACCEPTANCE
    assert created["org_id"] == org_id
    assert created["location_id"] == location_id
    assert created["order_type"] == "domicilio"
    assert created["subtotal"] == Decimal("40000")
    assert created["delivery_fee"] == Decimal("3000")
    assert created["tip_amount"] == Decimal("2000")
    assert created["total"] == Decimal("45000")
    assert created["paid"] is False
    assert created["customer_name"] == "Ana"
    assert created["customer_phone"] == "3001234567"
    assert created["customer_email"] == "ana@example.com"
    assert created["cash_change_for"] == Decimal("50000")
    assert created["items"] == _SAMPLE_ITEMS
    assert created["public_code"] is None  # claimed separately


async def test_count_open_orders_excludes_terminal_statuses(db_conn):
    from app.repositories.delivery_repo import db_count_open_orders_for_phone

    org_id = await _seed_org(db_conn, "Open Orders Org")
    location_id = await _seed_location(db_conn, org_id)
    phone = "3009998877"

    async def _seed(status: str):
        await _set_scope(db_conn, org_id)
        await db_conn.execute(
            """INSERT INTO orders
                   (id, org_id, location_id, phone, bot_number, order_type, status,
                    items, subtotal, total, customer_phone)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)""",
            f"ord-{uuid.uuid4().hex[:10]}", org_id, location_id, f"web:{uuid.uuid4()}",
            "573000000000", "domicilio", status, [], Decimal("10000"), Decimal("10000"), phone,
        )

    with tenant_scope(org_id):
        await _seed("pendiente_aceptacion")
        await _seed("en_preparacion")
        await _seed("entregado")
        await _seed("cancelado")
        await _seed("rechazado")

        count = await db_count_open_orders_for_phone(org_id, phone)

    assert count == 2  # only pendiente_aceptacion + en_preparacion are "open"


async def test_tenant_isolation_open_orders_count_never_crosses_orgs(db_conn):
    """Deliberate id collision (same shape as test_delivery_repo.py's
    `collision` fixture): org_b's own id equals a location that belongs to
    org_a. Counting org_b's open orders must never see org_a's rows."""
    from app.repositories.delivery_repo import db_count_open_orders_for_phone, db_create_delivery_order

    org_a = await _seed_org(db_conn, "Collision Count Org A")
    org_b = await _seed_org(db_conn, "Collision Count Org B")
    loc_l = await _seed_location(db_conn, org_a, "A sede (id collides with org B)", loc_id=org_b)
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"
    loc_b = await _seed_location(db_conn, org_b, "B real sede")

    shared_phone = "3005551111"

    with tenant_scope(org_a):
        await db_create_delivery_order(
            org_id=org_a, location_id=loc_l, phone="web:a1", bot_number="573000000001",
            order_type="domicilio", items=[], address="A", subtotal=Decimal("10000"),
            delivery_fee=Decimal("0"), tip_amount=Decimal("0"), total=Decimal("10000"),
            payment_method="efectivo", cash_change_for=None, customer_name="A",
            customer_phone=shared_phone, customer_email=None, delivery_lat=None, delivery_lon=None,
            proof_url=None, scheduled_pickup_at=None,
        )

    with tenant_scope(org_b):
        count_b_before = await db_count_open_orders_for_phone(org_b, shared_phone)
        await db_create_delivery_order(
            org_id=org_b, location_id=loc_b, phone="web:b1", bot_number="573000000002",
            order_type="recoger", items=[], address="B", subtotal=Decimal("10000"),
            delivery_fee=Decimal("0"), tip_amount=Decimal("0"), total=Decimal("10000"),
            payment_method="efectivo", cash_change_for=None, customer_name="B",
            customer_phone=shared_phone, customer_email=None, delivery_lat=None, delivery_lon=None,
            proof_url=None, scheduled_pickup_at=None,
        )
        count_b_after = await db_count_open_orders_for_phone(org_b, shared_phone)

    assert count_b_before == 0, "org B must never count org A's order even though the phone matches"
    assert count_b_after == 1


# ── C. HTTP-level tests (TestClient against the real app) ──────────────────


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


async def _http_seed_checkout_org(
    *, radius_km=50, payment_methods=("efectivo", "nequi"), min_order=0,
    delivery_fee=3000, pickup_enabled=True, delivery_enabled=True, opening_hours=None,
) -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        bot_number = f"573{suffix[:9]}"
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            f"Checkout Org {suffix}", f"checkout-org-{suffix}", json.dumps({"currency": "COP"}),
        )
        location_id = await conn.fetchval(
            """
            INSERT INTO locations
                (org_id, name, whatsapp_number, latitude, longitude, phone, address,
                 delivery_config, opening_hours)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
            RETURNING id
            """,
            org_id, f"Sede {suffix}", bot_number, 4.6097, -74.0817, "3011234567", "Cra 1 # 2-3",
            json.dumps({
                "delivery_enabled": delivery_enabled, "pickup_enabled": pickup_enabled,
                "delivery_fee": delivery_fee, "min_order": min_order, "radius_km": radius_km,
                "payment_methods": list(payment_methods),
            }),
            json.dumps(opening_hours or {}),
        )
        return {"org_id": org_id, "location_id": location_id, "bot_number": bot_number, "suffix": suffix}
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


async def _seed_open_order(org_id: int, location_id: int, bot_number: str, customer_phone: str, status: str) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute(
            """INSERT INTO orders
                   (id, org_id, location_id, phone, bot_number, order_type, status,
                    items, subtotal, total, customer_phone)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)""",
            f"ord-{uuid.uuid4().hex[:10]}", org_id, location_id, f"web:{uuid.uuid4()}",
            bot_number, "domicilio", status, json.dumps([]), Decimal("10000"), Decimal("10000"), customer_phone,
        )
    finally:
        await conn.close()


async def _fetch_order_by_id(order_id: str) -> dict | None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id = $1", order_id)
        return dict(row) if row else None
    finally:
        await conn.close()


async def _count_orders_for_phone(org_id: int, customer_phone: str) -> int:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE org_id = $1 AND customer_phone = $2", org_id, customer_phone,
        )
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
        await conn.execute("DELETE FROM orders WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM carts WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def checkout_org():
    info = _run(_http_seed_checkout_org())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


def _default_checkout_body(token: str, **overrides) -> dict:
    body = {
        "token": token,
        "idempotency_key": uuid.uuid4().hex,
        "customer_name": "Ana Pérez",
        "customer_phone": "3001112233",
        "address": "Calle 10 # 5-20, Barrio Centro",
        "lat": 4.6097,
        "lon": -74.0817,
        "payment_method": "efectivo",
        "cash_change_for": 100000.0,
        "tip_amount": 0.0,
    }
    body.update(overrides)
    return body


def test_checkout_delivery_happy_path_totals_and_fields(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    _run(_seed_cart(token, checkout_org["bot_number"], checkout_org["org_id"], [
        {"name": "Bandeja Paisa", "quantity": 2, "subtotal": 40000.0, "line_id": "a1"},
    ]))

    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token, tip_amount=1000.0, payment_method="efectivo", cash_change_for=100000.0,
    ))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["subtotal"] == 40000.0
    assert data["delivery_fee"] == 3000.0
    assert data["tip_amount"] == 1000.0
    assert data["total"] == 44000.0
    assert data["status"] == "pendiente_aceptacion"
    assert data["order_type"] == "domicilio"
    assert data["public_code"]

    row = _run(_fetch_order_by_id(data["order_id"]))
    assert row is not None
    assert row["org_id"] == checkout_org["org_id"]
    assert row["location_id"] == checkout_org["location_id"]
    assert row["status"] == "pendiente_aceptacion"
    assert row["customer_name"] == "Ana Pérez"
    assert row["customer_phone"] == "3001112233"
    assert row["public_code"] == data["public_code"]
    assert Decimal(str(row["subtotal"])) == Decimal("40000")
    assert Decimal(str(row["delivery_fee"])) == Decimal("3000")
    assert Decimal(str(row["total"])) == Decimal("44000")


def test_checkout_pickup_gets_zero_delivery_fee(client):
    info = _run(_http_seed_checkout_org(delivery_fee=5000))
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"], "pickup"))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 12000.0, "line_id": "b1"},
        ]))

        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
            token, lat=None, lon=None, cash_change_for=20000.0,
        ))
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["delivery_fee"] == 0.0
        assert data["order_type"] == "recoger"

        row = _run(_fetch_order_by_id(data["order_id"]))
        assert Decimal(str(row["delivery_fee"])) == Decimal("0")
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_idempotent_same_key_yields_one_order(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    _run(_seed_cart(token, checkout_org["bot_number"], checkout_org["org_id"], [
        {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 20000.0, "line_id": "a1"},
    ]))
    body = _default_checkout_body(token, idempotency_key="same-key-123")

    resp1 = _post(client, "/api/diner/delivery/checkout", json=body)
    assert resp1.status_code == 200, resp1.text
    resp2 = _post(client, "/api/diner/delivery/checkout", json=body)
    assert resp2.status_code == 200, resp2.text

    assert resp1.json() == resp2.json()
    assert resp1.json()["order_id"] == resp2.json()["order_id"]

    count = _run(_count_orders_for_phone(checkout_org["org_id"], "3001112233"))
    assert count == 1, "the same idempotency_key must never create a second order"


def test_checkout_refused_below_minimum(client):
    info = _run(_http_seed_checkout_org(min_order=100000))
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"], "delivery"))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 5000.0, "line_id": "c1"},
        ]))
        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "below_minimum"
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_refused_out_of_coverage(client):
    info = _run(_http_seed_checkout_org(radius_km=1))
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"], "delivery"))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
        ]))
        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
            token, lat=10.0, lon=-74.0,  # far outside the 1km radius around 4.6097,-74.0817
        ))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "out_of_coverage"
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_refused_sede_closed(client):
    closed_hours = {d: {"open": None, "close": None, "closed": True} for d in (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    )}
    info = _run(_http_seed_checkout_org(opening_hours=closed_hours))
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"], "delivery"))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
        ]))
        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "all_closed"
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_refused_next_day_schedule(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    _run(_seed_cart(token, checkout_org["bot_number"], checkout_org["org_id"], [
        {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
    ]))
    tomorrow = (datetime.now(dt_timezone.utc) + timedelta(days=1)).isoformat()
    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token, scheduled_for=tomorrow,
    ))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "schedule_not_today"


def test_checkout_refused_disallowed_payment_method(client):
    info = _run(_http_seed_checkout_org(payment_methods=("efectivo",)))
    try:
        token = _run(_seed_session(info["org_id"], info["location_id"], info["bot_number"], "delivery"))
        _run(_seed_cart(token, info["bot_number"], info["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
        ]))
        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
            token, payment_method="nequi", cash_change_for=None,
        ))
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "payment_method_not_allowed"
    finally:
        _run(_teardown_org(info["org_id"]))


def test_checkout_refused_cash_change_insufficient(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    _run(_seed_cart(token, checkout_org["bot_number"], checkout_org["org_id"], [
        {"name": "Bandeja Paisa", "quantity": 1, "subtotal": 40000.0, "line_id": "a1"},
    ]))
    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token, payment_method="efectivo", cash_change_for=10000.0,  # total will be 43000
    ))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "cash_change_insufficient"


def test_checkout_refused_dine_in_session_token(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "dine_in"))
    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "invalid_session_mode"


def test_checkout_refused_empty_cart(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "empty_cart"


def test_checkout_refused_too_many_open_orders(client, checkout_org):
    phone = "3007778899"
    for _ in range(3):  # _MAX_OPEN_ORDERS_PER_PHONE in app/routes/diner_delivery.py
        _run(_seed_open_order(
            checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"],
            phone, "pendiente_aceptacion",
        ))

    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    _run(_seed_cart(token, checkout_org["bot_number"], checkout_org["org_id"], [
        {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
    ]))
    resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token, customer_phone=phone,
    ))
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["reason"] == "too_many_open_orders"


def test_checkout_tenant_isolation_with_colliding_ids(client):
    """org_b's own id is forced to equal a location that belongs to org_a
    (same collision shape as test_delivery_entry.py / test_delivery_repo.py).
    Checking out against org_a's slug/session must only ever touch org_a's
    rows, never org_b's, even though the numeric ids coincide."""
    suffix = uuid.uuid4().hex[:8]

    async def _seed():
        # All connection use happens inside ONE coroutine / one event loop —
        # an asyncpg connection is bound to the loop it was created on (see
        # tests/test_delivery_entry.py's _seed_colliding_orgs docstring).
        conn = await asyncpg.connect(TEST_DB_URL)
        try:
            org_a = await conn.fetchval(
                "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
                f"Checkout Collision A {suffix}", f"checkout-collision-a-{suffix}", json.dumps({"currency": "COP"}),
            )
            org_b = await conn.fetchval(
                "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id",
                f"Checkout Collision B {suffix}", f"checkout-collision-b-{suffix}",
            )
            bot_number = f"573{suffix}0"
            loc_l = await conn.fetchval(
                """INSERT INTO locations (id, org_id, name, whatsapp_number, latitude, longitude, delivery_config)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb) RETURNING id""",
                org_b, org_a, "A sede (collides with org B id)", bot_number, 4.6097, -74.0817,
                json.dumps({"delivery_enabled": True, "pickup_enabled": True, "radius_km": 50, "payment_methods": ["efectivo"]}),
            )
            # A forced explicit id does NOT advance locations' BIGSERIAL
            # sequence — without this the sequence eventually reaches it and
            # the next ordinary INSERT anywhere in the suite dies on
            # locations_pkey (passes alone, fails in a full run).
            await _advance_location_seq(conn, loc_l)
            return org_a, org_b, loc_l, bot_number
        finally:
            await conn.close()

    org_a, org_b, loc_l, bot_number = _run(_seed())
    assert loc_l == org_b, "setup invariant broken: location id must equal org_b's id"

    try:
        token = _run(_seed_session(org_a, loc_l, bot_number, "delivery"))
        _run(_seed_cart(token, bot_number, org_a, [
            {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
        ]))
        resp = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(token))
        assert resp.status_code == 200, resp.text
        data = resp.json()

        row = _run(_fetch_order_by_id(data["order_id"]))
        assert row["org_id"] == org_a
        assert row["location_id"] == loc_l

        # org_b must see nothing from this order (RLS + explicit org_id filter).
        count_b = _run(_count_orders_for_phone(org_b, "3001112233"))
        assert count_b == 0
    finally:
        _run(_teardown_org(org_a))
        _run(_teardown_org(org_b))


def test_payment_proof_upload_requires_valid_session(client, checkout_org):
    resp = _post(
        client, "/api/diner/delivery/payment-proof",
        data={"token": "web:does-not-exist"},
        files={"file": ("proof.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
    )
    assert resp.status_code == 404


def test_payment_proof_upload_rejects_dine_in_session(client, checkout_org):
    token = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "dine_in"))
    resp = _post(
        client, "/api/diner/delivery/payment-proof",
        data={"token": token},
        files={"file": ("proof.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
    )
    assert resp.status_code == 422


def test_payment_proof_only_attaches_to_the_uploading_sessions_own_order(client, checkout_org, monkeypatch):
    """The checkout request body has NO proof_url field — the only way a
    proof URL reaches an order is by having been uploaded, moments earlier,
    through THIS SAME token's own /payment-proof call."""
    from app.services import image_host

    fake_urls = iter([
        "https://res.cloudinary.com/mesio/fake/session_a_proof.jpg",
    ])
    monkeypatch.setattr(
        image_host, "upload_delivery_proof",
        lambda org_id, data, content_type: {"secure_url": next(fake_urls), "public_id": "fake"},
    )

    token_a = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    token_b = _run(_seed_session(checkout_org["org_id"], checkout_org["location_id"], checkout_org["bot_number"], "delivery"))
    for t in (token_a, token_b):
        _run(_seed_cart(t, checkout_org["bot_number"], checkout_org["org_id"], [
            {"name": "Jugo", "quantity": 1, "subtotal": 20000.0, "line_id": "c1"},
        ]))

    upload_resp = _post(
        client, "/api/diner/delivery/payment-proof",
        data={"token": token_a},
        files={"file": ("proof.png", b"\x89PNG\r\n\x1a\nrest", "image/png")},
    )
    assert upload_resp.status_code == 200, upload_resp.text
    uploaded_url = upload_resp.json()["proof_url"]

    # Session B checks out WITHOUT ever uploading anything itself.
    resp_b = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token_b, idempotency_key="b-key", payment_method="nequi", cash_change_for=None,
    ))
    assert resp_b.status_code == 200, resp_b.text
    assert resp_b.json()["proof_url"] is None, "session B must never inherit session A's uploaded proof"

    # Session A checks out — its OWN uploaded proof must be attached.
    resp_a = _post(client, "/api/diner/delivery/checkout", json=_default_checkout_body(
        token_a, idempotency_key="a-key", payment_method="nequi", cash_change_for=None,
    ))
    assert resp_a.status_code == 200, resp_a.text
    assert resp_a.json()["proof_url"] == uploaded_url

    row_a = _run(_fetch_order_by_id(resp_a.json()["order_id"]))
    assert row_a["proof_url"] == uploaded_url
