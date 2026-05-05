"""
tests/e2e/test_staff_pin_login_e2e.py — E2E test: POST /api/staff/pin-login.

Regression test for the tenant_scope bug discovered on 2026-05-05:
  db_get_staff_for_pin_login() calls tenant_connection() which raises
  TenantNotSetError if the route does not establish tenant_scope() first.
  Fix in app/routes/staff.py wraps the lookup in tenant_scope(restaurant_id).

If somebody removes the tenant_scope wrapper from /pin-login, this test
fails with a 500 (TenantNotSetError) instead of the expected 200 + token,
giving CI a clear, immediate signal.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from passlib.context import CryptContext

import asyncpg

from tests.e2e.conftest import seed_restaurant, truncate_e2e_data
from app.services.tenant_context import bypass_tenant_scope

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

STAFF_NAME = "Pin Login E2E"
STAFF_USERNAME_OK = "pin.login.e2e.ok"
STAFF_USERNAME_BAD = "pin.login.e2e.bad"
STAFF_PIN = "4321"


@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_staff_pin_login_returns_token(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture,
):
    """
    Seed staff with a known PIN -> POST /api/staff/pin-login -> assert token returned.

    Mutation hint: remove `with tenant_scope(body.restaurant_id):` from
    staff_pin_login() in app/routes/staff.py. This test will fail with HTTP 500
    (TenantNotSetError) on the db_get_staff_for_pin_login call.
    """
    pool = test_pool
    pin_hash = _pwd_ctx.hash(STAFF_PIN)

    restaurant = await seed_restaurant(
        pool,
        name="E2E Pin Login Test Restaurant",
        bot_number_raw="+570E2EPINLOG",
        num_branches=0,
    )
    org_id = restaurant["id"]

    await truncate_e2e_data(pool, org_id)

    with bypass_tenant_scope("e2e_pin_login_seed"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            # username is GLOBAL UNIQUE — clean cross-org leftovers from prior runs
            await conn.execute(
                "DELETE FROM staff WHERE username = $1", STAFF_USERNAME_OK,
            )
            await conn.execute(
                """INSERT INTO staff
                     (org_id, name, username, role, roles, pin, phone, hourly_rate, active)
                   VALUES ($1, $2, $3, 'mesero', '["mesero"]'::jsonb, $4,
                           '3009998001', 20000, true)""",
                org_id, STAFF_NAME, STAFF_USERNAME_OK, pin_hash,
            )

    # ── Real POST /api/staff/pin-login ───────────────────────────────────────
    resp = await e2e_app.post(
        "/api/staff/pin-login",
        json={
            "restaurant_id": org_id,
            "name": STAFF_USERNAME_OK,
            "pin": STAFF_PIN,
        },
    )
    assert resp.status_code == 200, (
        f"POST /api/staff/pin-login failed: {resp.status_code} {resp.text}. "
        "If status is 500, the tenant_scope wrapper around db_get_staff_for_pin_login "
        "is missing — staff cannot log in via PIN. See app/routes/staff.py::staff_pin_login."
    )

    data = resp.json()
    # LOAD-BEARING: the response must include a usable session token.
    assert data.get("token"), f"pin-login response missing 'token' field: {data!r}"
    assert data.get("staff_id"), f"pin-login response missing 'staff_id' field: {data!r}"
    assert data.get("name") == STAFF_NAME, (
        f"pin-login returned wrong name: expected {STAFF_NAME!r}, got {data.get('name')!r}"
    )
    assert "mesero" in (data.get("roles") or []), (
        f"pin-login response missing 'mesero' role: {data!r}"
    )
    assert data.get("redirect"), (
        f"pin-login response missing 'redirect' field — staff client cannot route post-login: {data!r}"
    )

    # The token must actually authenticate downstream (proves session was created).
    profile_resp = await e2e_app.get(
        "/api/staff/self/profile",
        headers={"Authorization": f"Bearer {data['token']}"},
    )
    assert profile_resp.status_code == 200, (
        f"Token from pin-login does not authenticate /api/staff/self/profile: "
        f"{profile_resp.status_code} {profile_resp.text}"
    )


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_staff_pin_login_wrong_pin_returns_401(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture,
):
    """Wrong PIN must return 401 with a constant-time response (no enumeration)."""
    pool = test_pool
    pin_hash = _pwd_ctx.hash(STAFF_PIN)

    restaurant = await seed_restaurant(
        pool,
        name="E2E Pin Login Wrong Pin Restaurant",
        bot_number_raw="+570E2EPINBAD",
        num_branches=0,
    )
    org_id = restaurant["id"]

    await truncate_e2e_data(pool, org_id)

    with bypass_tenant_scope("e2e_pin_login_wrong_seed"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                "DELETE FROM staff WHERE username = $1", STAFF_USERNAME_BAD,
            )
            await conn.execute(
                """INSERT INTO staff
                     (org_id, name, username, role, roles, pin, phone, hourly_rate, active)
                   VALUES ($1, $2, $3, 'mesero', '["mesero"]'::jsonb, $4,
                           '3009998002', 20000, true)""",
                org_id, STAFF_NAME, STAFF_USERNAME_BAD, pin_hash,
            )

    resp = await e2e_app.post(
        "/api/staff/pin-login",
        json={
            "restaurant_id": org_id,
            "name": STAFF_USERNAME_BAD,
            "pin": "0000",
        },
    )
    assert resp.status_code == 401, (
        f"Wrong PIN should return 401, got {resp.status_code}: {resp.text}"
    )
