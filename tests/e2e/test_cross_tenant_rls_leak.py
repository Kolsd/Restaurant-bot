"""
tests/e2e/test_cross_tenant_rls_leak.py — E2E test: Fase 1 RLS tenant isolation.

ONE test that proves the multi-tenant blindaje works end-to-end:
  - Admin A can see org A's orders via GET /api/kitchen/delivery-orders
  - Admin A CANNOT see org B's orders in that same response
  - Admin A CANNOT mutate org B's order via PATCH /api/delivery/orders/{B_id}/status
  - After the cross-tenant PATCH attempt, org B's order status must be unchanged in DB

No bot flows are run — this is a pure data-layer / API-auth RLS test.

Setup:
  - Two separate orgs seeded with distinct bot numbers.
  - One delivery order seeded per org directly in DB via bypass_tenant_scope.
  - Two admin session tokens, one per org owner.

Assertions:
  1. admin_A sees order_A in kitchen list, order_B absent.
  2. admin_B sees order_B in kitchen list, order_A absent.
  3. admin_A PATCH order_B → non-200 (403 or 404).
  4. DB status of order_B unchanged after the PATCH attempt.

Prerequisites:
  ANTHROPIC_API_KEY is NOT required — no LLM calls.
  TEST_DATABASE_URL   — isolated Postgres test DB
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    create_admin_token,
    seed_restaurant,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


# ── App fixture (no wa_capture needed — no bot turns) ─────────────────────────

@pytest_asyncio.fixture()
async def e2e_app_rls(wa_capture):
    """FastAPI app via ASGI transport without bot turns."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=60.0,
        ) as client:
            yield client


# ── Helper: seed one delivery order directly in DB ────────────────────────────

async def _seed_delivery_order(pool: asyncpg.Pool, org_id: int, bot_number: str) -> str:
    """
    Insert a minimal delivery order for the given org directly in DB.
    Uses bypass_tenant_scope so we can write for both orgs from the same function.
    Returns the generated order_id (UUID string).
    """
    order_id = str(uuid.uuid4())
    phone = f"3001110{org_id:04d}"  # unique-enough per org
    items_json = json.dumps([{"name": "Empanaditas de Carne (3 uds)", "quantity": 1, "price": 15000}])
    status = "pendiente"

    # created_at without timezone (TIMESTAMP WITHOUT TIME ZONE in schema per CLAUDE.md Note #5)
    created_at = datetime.utcnow().replace(tzinfo=None)

    with bypass_tenant_scope("e2e_rls_seed_cross"):
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders (
                    id, phone, items, order_type, address, notes,
                    subtotal, delivery_fee, total, status, paid,
                    payment_url, bot_number, payment_method,
                    base_order_id, sub_number, org_id, channel,
                    created_at
                ) VALUES (
                    $1, $2, $3::jsonb, $4, $5, $6,
                    $7, $8, $9, $10, $11,
                    $12, $13, $14,
                    $15, $16, $17, $18,
                    $19
                )
                ON CONFLICT (id) DO NOTHING
                """,
                order_id,              # $1  id
                phone,                 # $2  phone
                items_json,            # $3  items
                "domicilio",           # $4  order_type
                "Calle E2E RLS 123",   # $5  address
                "",                    # $6  notes
                15000,                 # $7  subtotal
                0,                     # $8  delivery_fee
                15000,                 # $9  total
                status,                # $10 status
                False,                 # $11 paid
                "",                    # $12 payment_url
                bot_number,            # $13 bot_number
                "Nequi",               # $14 payment_method
                None,                  # $15 base_order_id
                1,                     # $16 sub_number
                org_id,                # $17 org_id  (tenant key)
                "whatsapp",            # $18 channel
                created_at,            # $19 created_at
            )
    log.info("e2e.rls.order_seeded", order_id=order_id, org_id=org_id, phone=phone)
    return order_id


# ── The single E2E test ────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_admin_cannot_see_other_tenant_orders(
    test_pool: asyncpg.Pool,
    e2e_app_rls: AsyncClient,
    wa_capture: WACapture,
):
    """
    Proves Fase 1 RLS blindaje works end-to-end for delivery orders.

    Verified invariants:
      A) admin_A GET /api/kitchen/delivery-orders → sees order_A, NOT order_B.
      B) admin_B GET /api/kitchen/delivery-orders → sees order_B, NOT order_A.
      C) admin_A PATCH /api/delivery/orders/{order_B_id}/status → 403 or 404.
      D) After (C), order_B status in DB == 'pendiente' (unchanged).

    If any invariant breaks → RLS leak → test fails loudly.
    """
    client = e2e_app_rls
    pool = test_pool

    # ── Seed two distinct restaurants (orgs) ──────────────────────────────────
    rest_A = await seed_restaurant(
        pool,
        name="E2E RLS Tenant A",
        bot_number_raw="+570E2ETENANTA",
        num_branches=1,
        branch_latlons=[(4.710989, -74.072092)],
    )
    rest_B = await seed_restaurant(
        pool,
        name="E2E RLS Tenant B",
        bot_number_raw="+570E2ETENANTB",
        num_branches=1,
        branch_latlons=[(4.609710, -74.081741)],
    )

    org_id_A = rest_A["id"]
    org_id_B = rest_B["id"]

    # Critical: the two orgs MUST be distinct — otherwise the test proves nothing.
    assert org_id_A != org_id_B, (
        f"seed_restaurant returned the same org_id ({org_id_A}) for both tenants. "
        f"Check that the bot_number is truly unique per call."
    )
    log.info("e2e.rls.orgs_seeded", org_id_A=org_id_A, org_id_B=org_id_B)

    # Clean volatile tables for both orgs
    await truncate_e2e_data(pool, org_id_A)
    await truncate_e2e_data(pool, org_id_B)
    for branch in rest_A["branches"]:
        await truncate_e2e_data(pool, branch["id"])
    for branch in rest_B["branches"]:
        await truncate_e2e_data(pool, branch["id"])

    # Reset in-process state_store fallback (no-op if Redis is available)
    try:
        import app.services.state_store as ss
        for attr in ["_fb_nps", "_fb_nps_done", "_fb_checkout", "_fb_cooldown",
                     "_fb_cart_locks", "_fb_cart_lock_tokens", "_fb_rate_limits"]:
            d = getattr(ss, attr, None)
            if d is not None and isinstance(d, dict):
                d.clear()
    except Exception:
        pass

    # ── Seed one order per org directly in DB ─────────────────────────────────
    order_id_A = await _seed_delivery_order(pool, org_id_A, rest_A["whatsapp_number"])
    order_id_B = await _seed_delivery_order(pool, org_id_B, rest_B["whatsapp_number"])

    log.info(
        "e2e.rls.orders_seeded",
        order_id_A=order_id_A,
        order_id_B=order_id_B,
        org_id_A=org_id_A,
        org_id_B=org_id_B,
    )

    # Verify both orders are in DB (bypass check)
    with bypass_tenant_scope("e2e_rls_verify_seed"):
        async with pool.acquire() as conn:
            row_A = await conn.fetchrow("SELECT org_id, status FROM orders WHERE id = $1", order_id_A)
            row_B = await conn.fetchrow("SELECT org_id, status FROM orders WHERE id = $1", order_id_B)

    assert row_A is not None, f"Seed failed: order_A ({order_id_A}) not in DB"
    assert row_B is not None, f"Seed failed: order_B ({order_id_B}) not in DB"
    assert row_A["org_id"] == org_id_A, f"order_A has wrong org_id: {row_A['org_id']} != {org_id_A}"
    assert row_B["org_id"] == org_id_B, f"order_B has wrong org_id: {row_B['org_id']} != {org_id_B}"

    # ── Create admin session tokens for both owners ────────────────────────────
    token_A = await create_admin_token(pool, rest_A["owner_email"])
    token_B = await create_admin_token(pool, rest_B["owner_email"])

    headers_A = {"Authorization": f"Bearer {token_A}"}
    headers_B = {"Authorization": f"Bearer {token_B}"}

    # ── Assertion A: admin_A sees order_A, NOT order_B ────────────────────────
    log.info("e2e.rls.assert_A_sees_only_A")
    resp_A = await client.get("/api/kitchen/delivery-orders", headers=headers_A)
    assert resp_A.status_code == 200, (
        f"admin_A GET kitchen orders failed: {resp_A.status_code} {resp_A.text}"
    )
    orders_A = resp_A.json().get("orders", [])
    ids_A = {str(o["id"]) for o in orders_A}
    log.info("e2e.rls.admin_A_orders", ids=ids_A, count=len(ids_A))

    # admin_A must see order_A
    assert str(order_id_A) in ids_A, (
        f"RLS FAIL: admin_A cannot see its own order_A ({order_id_A}). "
        f"Orders visible to A: {ids_A}. "
        "Possible cause: RLS policy blocking A's own data."
    )
    # admin_A must NOT see order_B
    assert str(order_id_B) not in ids_A, (
        f"RLS LEAK: admin_A can see order_B ({order_id_B}) which belongs to org_B. "
        f"All orders in response: {ids_A}. "
        "CRITICAL: tenant isolation broken — org_A reads org_B data."
    )

    # ── Assertion B: admin_B sees order_B, NOT order_A ────────────────────────
    log.info("e2e.rls.assert_B_sees_only_B")
    resp_B = await client.get("/api/kitchen/delivery-orders", headers=headers_B)
    assert resp_B.status_code == 200, (
        f"admin_B GET kitchen orders failed: {resp_B.status_code} {resp_B.text}"
    )
    orders_B = resp_B.json().get("orders", [])
    ids_B = {str(o["id"]) for o in orders_B}
    log.info("e2e.rls.admin_B_orders", ids=ids_B, count=len(ids_B))

    # admin_B must see order_B
    assert str(order_id_B) in ids_B, (
        f"RLS FAIL: admin_B cannot see its own order_B ({order_id_B}). "
        f"Orders visible to B: {ids_B}. "
        "Possible cause: RLS policy blocking B's own data."
    )
    # admin_B must NOT see order_A
    assert str(order_id_A) not in ids_B, (
        f"RLS LEAK: admin_B can see order_A ({order_id_A}) which belongs to org_A. "
        f"All orders in response: {ids_B}. "
        "CRITICAL: tenant isolation broken — org_B reads org_A data."
    )

    # ── Assertion C: admin_A PATCH order_B → non-200 ─────────────────────────
    log.info("e2e.rls.cross_tenant_patch_attempt", actor="admin_A", target_order=order_id_B)
    patch_resp = await client.patch(
        f"/api/delivery/orders/{order_id_B}/status",
        json={"status": "confirmado"},
        headers=headers_A,
    )
    log.info(
        "e2e.rls.cross_tenant_patch_result",
        status_code=patch_resp.status_code,
        body=patch_resp.text[:200],
    )

    # The endpoint MUST deny this — 403 (ownership check) or 404 (order not visible).
    # A 200 with DB change is a catastrophic RLS failure.
    patch_status = patch_resp.status_code

    # ── Assertion D: order_B status unchanged in DB ───────────────────────────
    with bypass_tenant_scope("e2e_rls_verify_unchanged"):
        async with pool.acquire() as conn:
            row_B_after = await conn.fetchrow(
                "SELECT status FROM orders WHERE id = $1", order_id_B
            )

    assert row_B_after is not None, f"order_B ({order_id_B}) disappeared from DB"
    db_status_after = row_B_after["status"]
    log.info(
        "e2e.rls.order_B_status_after_patch",
        db_status=db_status_after,
        patch_http_status=patch_status,
    )

    # Check combined: either HTTP said no, or DB was untouched.
    # If 200 AND DB changed → catastrophic RLS leak.
    if patch_status == 200 and db_status_after != "pendiente":
        pytest.fail(
            f"CRITICAL RLS LEAK: admin_A successfully mutated order_B "
            f"(order_id={order_id_B}, org_id_B={org_id_B}). "
            f"PATCH returned 200 and DB status changed to '{db_status_after}'. "
            f"Ownership check at orders_routes.py:437 was bypassed. "
            f"This is a catastrophic multi-tenant security failure."
        )

    # Preferred outcome: HTTP denied it (403/404)
    assert patch_status in (403, 404), (
        f"Expected 403 or 404 when admin_A patches order_B. "
        f"Got {patch_status}. Body: {patch_resp.text[:400]}. "
        f"DB status after: {db_status_after!r} (was 'pendiente'). "
        f"Even if DB is unchanged, the API returning {patch_status} is unexpected — "
        f"check the ownership guard in update_delivery_status."
    )

    # DB must remain pendiente regardless of HTTP status
    assert db_status_after == "pendiente", (
        f"order_B status changed from 'pendiente' to '{db_status_after}' "
        f"after cross-tenant PATCH by admin_A. "
        f"HTTP status was {patch_status}. "
        f"RLS or ownership check failed to prevent the mutation."
    )

    log.info(
        "e2e.rls.test_passed",
        org_id_A=org_id_A,
        org_id_B=org_id_B,
        order_id_A=order_id_A,
        order_id_B=order_id_B,
        cross_tenant_patch_status=patch_status,
        order_B_db_status_unchanged=True,
    )
    print(
        f"\n[OK] E2E RLS test passed: "
        f"org_A ({org_id_A}) and org_B ({org_id_B}) are fully isolated. "
        f"Cross-tenant PATCH returned HTTP {patch_status}. "
        f"order_B status unchanged in DB ('pendiente')."
    )
