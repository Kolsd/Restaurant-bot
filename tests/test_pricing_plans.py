"""
tests/test_pricing_plans.py
===========================
The 2026-09-30 price list (docs/claude/status.md #15, app/services/plans.py):
flat price per sede, 15-day trial of Restaurante, founder program 40% off
frozen for life, 10 spots.

Pure checks on the catalog run everywhere; the DB tests need TEST_DATABASE_URL
and migration 0101, and run inside a transaction that is rolled back.
"""
import os
import pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import asyncpg
import pytest
from fastapi import HTTPException, Request

from app.services import plans

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="TEST_DATABASE_URL not set")

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _cop(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


# ── The catalog ──────────────────────────────────────────────────────────────


def test_founder_prices_are_the_ones_the_landing_prints():
    assert {c: plans.founder_price(c) for c in plans.PLAN_ORDER} == {
        "esencial": 71_000, "restaurante": 149_000, "pro": 209_000, "cadena": 179_000,
    }


def test_landing_terms_and_signup_show_the_catalog_prices():
    """The public pages are hand-written HTML; this keeps them honest."""
    landing = (ROOT / "app/static/html/landing.html").read_text(encoding="utf-8")
    terms = (ROOT / "app/static/html/terms.html").read_text(encoding="utf-8")
    signup = (ROOT / "app/static/html/signup.html").read_text(encoding="utf-8")
    for code in plans.PLAN_ORDER:
        price = _cop(plans.PRICES_COP[code])
        assert price in landing, f"landing is missing {code} at {price}"
        assert price in terms, f"terms are missing {code} at {price}"
        assert price in signup, f"signup is missing {code} at {price}"
        assert _cop(plans.founder_price(code)) in landing
    assert "15 días gratis" in landing


def test_trial_gives_at_least_restaurante():
    trial = NOW + timedelta(days=3)
    assert plans.effective_plan("esencial", trial, NOW) == "restaurante"
    assert plans.effective_plan("pro", trial, NOW) == "pro", "a trial never downgrades"
    assert plans.effective_plan("esencial", NOW - timedelta(seconds=1), NOW) == "esencial"
    assert plans.effective_plan("esencial", None, NOW) == "esencial"
    # Repos hand timestamps back as ISO strings, naive ones being UTC.
    assert plans.effective_plan("esencial", trial.isoformat(), NOW) == "restaurante"
    assert plans.effective_plan("esencial", trial.replace(tzinfo=None).isoformat(), NOW) == "restaurante"


def test_unknown_plan_codes_fall_back_to_the_base_plan():
    for stale in ("pulso", "free", "comp", "", None):
        assert plans.normalize_plan(stale) == "esencial"


def test_what_each_plan_unlocks():
    def has(plan, feature):
        return plans.has_feature({"plan_code": plan, "comp_until": None}, feature, NOW)

    assert not has("esencial", plans.AI_ASSISTANT)
    assert not has("esencial", plans.DELIVERY)
    assert has("restaurante", plans.AI_ASSISTANT) and has("restaurante", plans.DELIVERY)
    assert not has("restaurante", plans.RESERVATIONS)
    assert not has("restaurante", plans.DIAN)
    for plan in ("pro", "cadena"):
        for feature in (plans.AI_ASSISTANT, plans.DELIVERY, plans.RESERVATIONS,
                        plans.INVENTORY, plans.DIAN):
            assert has(plan, feature), (plan, feature)

    in_trial = {"plan_code": "esencial", "comp_until": NOW + timedelta(days=1)}
    assert plans.has_feature(in_trial, plans.AI_ASSISTANT, NOW)
    assert plans.staff_cap({"plan_code": "esencial", "comp_until": None}, NOW) == 5
    assert plans.staff_cap(in_trial, NOW) is None
    assert plans.staff_cap({"plan_code": "restaurante", "comp_until": None}, NOW) is None


def test_price_per_sede_prefers_the_frozen_founder_price():
    assert plans.monthly_price_per_sede("restaurante", None) == 249_000
    assert plans.monthly_price_per_sede("restaurante", 149_000) == 149_000


# ── DB fixtures (same proxy/shim pattern as test_plan_downgrade.py) ──────────


class _ConnProxy:
    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_c"), name)

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


@pytest.fixture
async def db_conn(monkeypatch):
    from app.services import database as db_module

    conn = await asyncpg.connect(TEST_DB_URL)
    await db_module.init_connection(conn)  # the app pool's jsonb codec
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
        await conn.close()


async def _new_org(conn, plan="restaurante", *, sedes=1, founder=None, comp_until=None):
    from app.services.tenant_context import bypass_tenant_scope

    with bypass_tenant_scope("test_pricing_setup"):
        await conn.execute("SET LOCAL ROLE mesio_superadmin")
        org_id = await conn.fetchval(
            """INSERT INTO organizations (name, plan_code, subscription_plan,
                                          founder_price_cop, comp_until)
               VALUES ('Pricing test', $1, $1, $2, $3) RETURNING id""",
            plan, founder, comp_until,
        )
        for i in range(sedes):
            await conn.execute(
                "INSERT INTO locations (org_id, name, code) VALUES ($1, $2, $3)",
                org_id, f"Sede {i}", f"pricing-{org_id}-{i}",
            )
        await conn.execute("SET LOCAL ROLE mesio_app")
    return org_id


async def _org(conn, org_id):
    from app.services.tenant_context import bypass_tenant_scope

    with bypass_tenant_scope("test_pricing_read"):
        await conn.execute("SET LOCAL ROLE mesio_superadmin")
        row = await conn.fetchrow(
            "SELECT plan_code, subscription_plan, founder_price_cop, comp_until "
            "FROM organizations WHERE id = $1", org_id,
        )
        await conn.execute("SET LOCAL ROLE mesio_app")
    return row


def _request():
    req = MagicMock(spec=Request)
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value="superadmin")
    return req


# ── DB tests ─────────────────────────────────────────────────────────────────


@needs_db
@pytest.mark.asyncio
async def test_plan_limits_mirrors_the_catalog(db_conn):
    rows = await db_conn.fetch("SELECT plan_code, monthly_price_cop FROM plan_limits")
    assert {r["plan_code"]: r["monthly_price_cop"] for r in rows} == plans.PRICES_COP


@needs_db
@pytest.mark.asyncio
async def test_orgs_created_without_a_plan_get_the_trial_plan(db_conn):
    """Signup, CRM and superadmin always name a plan; anything else (demo
    seeds, fixtures) lands on Restaurante, the plan the trial gives."""
    default = await db_conn.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'organizations' AND column_name = 'plan_code'"
    )
    assert plans.TRIAL_PLAN in default


@needs_db
@pytest.mark.asyncio
async def test_db_create_organization_stores_the_chosen_plan(db_conn):
    """Signup used to write only subscription_plan, leaving plan_code at its
    default: every self-serve restaurant was billed as the base plan."""
    from app.repositories import restaurant_repo

    org = await restaurant_repo.db_create_organization(name="Plan elegido", plan_code="pro")
    row = await _org(db_conn, org["id"])
    assert row["plan_code"] == "pro"
    assert row["subscription_plan"] == "pro"


@needs_db
@pytest.mark.asyncio
async def test_set_plan_keeps_a_founder_discount_on_the_new_plan(db_conn):
    from app.repositories import plan_limits_repo
    from app.services.tenant_context import bypass_tenant_scope

    founder = await _new_org(db_conn, "restaurante", founder=149_000)
    regular = await _new_org(db_conn, "restaurante")
    with bypass_tenant_scope("test_set_plan"):
        await plan_limits_repo.db_set_plan(founder, "pro")
        await plan_limits_repo.db_set_plan(regular, "pro")

    f, r = await _org(db_conn, founder), await _org(db_conn, regular)
    assert (f["plan_code"], f["subscription_plan"], f["founder_price_cop"]) == ("pro", "pro", 209_000)
    assert (r["plan_code"], r["founder_price_cop"]) == ("pro", None)


@needs_db
@pytest.mark.asyncio
async def test_founder_spots_run_out_at_ten(db_conn):
    from app.repositories import plan_limits_repo
    from app.services.tenant_context import bypass_tenant_scope

    taken = await db_conn.fetchval(
        "SELECT COUNT(*) FROM organizations WHERE founder_price_cop IS NOT NULL"
    )
    for _ in range(plans.FOUNDER_SPOTS - 1 - taken):
        await _new_org(db_conn, founder=149_000)

    last = await _new_org(db_conn, "esencial")
    one_too_many = await _new_org(db_conn, "restaurante")
    with bypass_tenant_scope("test_founder_spots"):
        assert await plan_limits_repo.db_set_founder(last, True) == 71_000
        assert await plan_limits_repo.db_set_founder(last, True) == 71_000, "idempotent"
        with pytest.raises(plan_limits_repo.FounderSpotsTaken):
            await plan_limits_repo.db_set_founder(one_too_many, True)
        # Leaving frees the spot for someone else.
        assert await plan_limits_repo.db_set_founder(last, False) is None
        assert await plan_limits_repo.db_set_founder(one_too_many, True) == 149_000

    assert (await _org(db_conn, last))["founder_price_cop"] is None


@needs_db
@pytest.mark.asyncio
async def test_mrr_is_price_per_sede_times_sedes(db_conn):
    from app.repositories.internal import mrr_repo
    from app.services.tenant_context import bypass_tenant_scope

    with bypass_tenant_scope("test_mrr_before"):
        before = await mrr_repo.db_compute_mrr()

    await _new_org(db_conn, "restaurante", sedes=2)                     # 2 × 249.000
    await _new_org(db_conn, "cadena", sedes=3, founder=179_000)          # 3 × 179.000
    await _new_org(db_conn, "pro", comp_until=datetime.now(timezone.utc) + timedelta(days=5))  # trial

    with bypass_tenant_scope("test_mrr_after"):
        after = await mrr_repo.db_compute_mrr()

    assert after["mrr_total_cop"] - before["mrr_total_cop"] == 2 * 249_000 + 3 * 179_000
    assert after["paying_count"] - before["paying_count"] == 2
    assert after["comp_count"] - before["comp_count"] == 1


@needs_db
@pytest.mark.asyncio
async def test_admin_endpoints_change_plan_free_days_and_founder(db_conn):
    from app.routes.internal.admin import (
        ChangePlanRequest, SetCompRequest, SetFounderRequest,
        change_org_plan, set_org_comp, set_org_founder,
    )
    from app.services.tenant_context import bypass_tenant_scope

    org_id = await _new_org(db_conn, "esencial")
    with bypass_tenant_scope("test_admin_plan_endpoints"):
        res = await change_org_plan(org_id=org_id, body=ChangePlanRequest(plan_code="restaurante"),
                                    request=_request())
        assert res["data"]["old_plan"] == "esencial"
        await set_org_comp(org_id=org_id, body=SetCompRequest(comp_until="2027-01-15"),
                           request=_request())
        res = await set_org_founder(org_id=org_id, body=SetFounderRequest(founder=True),
                                    request=_request())
        assert res["data"]["founder_price_cop"] == 149_000

    row = await _org(db_conn, org_id)
    assert row["plan_code"] == "restaurante"
    assert row["comp_until"].date().isoformat() == "2027-01-15"
    assert row["founder_price_cop"] == 149_000

    with bypass_tenant_scope("test_admin_plan_endpoints_clear"):
        await set_org_comp(org_id=org_id, body=SetCompRequest(comp_until=None), request=_request())
        with pytest.raises(HTTPException) as bad_date:
            await set_org_comp(org_id=org_id, body=SetCompRequest(comp_until="15/01/2027"),
                               request=_request())
    assert bad_date.value.status_code == 400
    row = await _org(db_conn, org_id)
    assert row["comp_until"] is None
    assert row["plan_code"] == "restaurante", "clearing free days never touches the plan"


# ── What each plan unlocks, at the routes ────────────────────────────────────


def test_reservations_inventory_and_dian_start_at_pro(client, monkeypatch):
    from tests.conftest import patch_auth

    patch_auth(monkeypatch, plan_code="restaurante")
    headers = {"Authorization": "Bearer t"}
    for method, url, body in (
        ("get", "/api/reservations", None),
        ("get", "/api/inventory", None),
        ("post", "/api/billing/emit", {"order_id": "o-1"}),
        ("post", "/api/billing/test-connection", None),
    ):
        resp = getattr(client, method)(url, headers=headers, **({"json": body} if body else {}))
        assert resp.status_code == 403, (url, resp.status_code, resp.text)
        assert "desde el plan Pro" in resp.json()["detail"], url


def test_esencial_caps_active_staff_at_five_per_sede(client, monkeypatch):
    from unittest.mock import AsyncMock

    from app.repositories import restaurant_repo
    from app.services import database as db
    from tests.conftest import patch_auth

    patch_auth(monkeypatch, plan_code="esencial")
    monkeypatch.setattr(restaurant_repo, "db_get_org_locations",
                        AsyncMock(return_value=[{"id": 1, "name": "Única"}]))
    created = AsyncMock(return_value={"id": "s-6", "name": "Sexto"})
    monkeypatch.setattr(db, "db_create_staff", created)
    body = {"name": "Sexto", "password": "1234", "role": "mesero"}
    headers = {"Authorization": "Bearer t"}

    five = [{"id": str(i), "active": True} for i in range(5)]
    monkeypatch.setattr(db, "db_get_staff", AsyncMock(return_value=five))
    resp = client.post("/api/staff", json=body, headers=headers)
    assert resp.status_code == 403
    assert "hasta 5 usuarios por sede" in resp.json()["detail"]
    created.assert_not_awaited()

    # An inactive member does not count against the cap.
    four_and_one_off = five[:4] + [{"id": "4", "active": False}]
    monkeypatch.setattr(db, "db_get_staff", AsyncMock(return_value=four_and_one_off))
    resp = client.post("/api/staff", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    created.assert_awaited_once()
