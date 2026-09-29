"""
tests/test_agent_branches_hint.py

Locks down the behavior introduced in commit 45ff590:
  - When restaurant_obj is a Matriz with NO branches, _build_enriched_user_message
    must inject a [UBICACION_UNICA: ...] block so the LLM never asks
    "¿de cuál sucursal?" on a single-location tenant.
  - When restaurant_obj is a Matriz WITH branches, it must inject the
    [SUCURSALES: ...] block (the multi-location case).
  - When restaurant_obj is a branch (parent_restaurant_id set) OR when
    table_context exists (dine-in), neither block is injected.

CLAUDE.md Regla #16 mandates these stay.
"""
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch


# ── Helpers ──────────────────────────────────────────────────────────────────

def _matriz(rid: int = 1) -> dict:
    return {
        "id": rid,
        "name": "Test Restaurant",
        "parent_restaurant_id": None,
    }


def _branch(rid: int, parent: int) -> dict:
    return {
        "id": rid,
        "name": "Branch",
        "parent_restaurant_id": parent,
    }


class _FakeTransitConn:
    """Stand-in for the connection yielded by tenant_connection()."""

    async def fetchrow(self, *a, **kw):
        return None


@asynccontextmanager
async def _fake_tenant_conn():
    yield _FakeTransitConn()


def _build_patches(branches_return):
    """Patch all DB / orders deps inside _build_enriched_user_message.

    IMPORTANT: _build_enriched_user_message's in-transit-order check (agent.py,
    "Check for in-transit delivery order") calls `async with _tenant_conn() as
    conn:` directly — it is NOT routed through app.services.database, so
    mocking agent.db.* alone does not cover it. Without patching agent._tenant_conn
    too, this "unit test" silently opens a REAL asyncpg connection pool whenever
    a real DATABASE_URL/TEST_DATABASE_URL is exported (the whole in-transit block
    is wrapped in try/except Exception, so the resulting error is swallowed and
    logged — the test still "passes", but it leaves a REAL pool cached in the
    process-global app.services.database._pool, bound to THIS test's short-lived
    pytest-asyncio event loop. Once that loop closes, the pool is permanently
    broken ("Event loop is closed") for every later test in the same session
    that reaches the real get_pool() path — e.g.
    tests/test_billing_subscription_routes.py::test_get_plans_real_db, which
    fails nondeterministically depending on suite ordering. Patching
    agent._tenant_conn keeps this test fully isolated, as intended.
    """
    from app.services import agent
    return [
        patch.object(agent.db, "db_get_history", AsyncMock(return_value=[])),
        patch.object(agent.orders, "cart_summary", AsyncMock(return_value="")),
        patch.object(agent.db, "db_get_menu_availability", AsyncMock(return_value={})),
        patch.object(agent.db, "db_get_menu", AsyncMock(return_value={"Pizzas": [{"name": "Margarita", "price": 10000}]})),
        patch.object(agent.db, "db_get_branches", AsyncMock(return_value=branches_return)),
        patch.object(agent, "_tenant_conn", _fake_tenant_conn),
    ]


async def _call_builder(restaurant_obj, table_context, branches_return, feats=None):
    """Drive _build_enriched_user_message with the given inputs and return the enriched message."""
    from app.services.agent import _build_enriched_user_message
    from app.services.tenant_context import bypass_tenant_scope
    feats = feats or {}
    patches = _build_patches(branches_return)
    for p in patches:
        p.start()
    try:
        # The builder opens a tenant_connection() for the in-transit check —
        # wrap in bypass to satisfy fail-closed RLS guard inside unit tests.
        with bypass_tenant_scope("test_branches_hint_unit_no_real_db"):
            enriched, _menu_url, _hist = await _build_enriched_user_message(
                user_message_clean="quiero pedir comida",
                user_phone="+573001234567",
                bot_number="573001112222",
                restaurant_obj=restaurant_obj,
                restaurant_name="Test Restaurant",
                feats=feats,
                payment_methods_text="Efectivo, Nequi",
                table_context=table_context,
                session_state={},
            )
    finally:
        for p in patches:
            p.stop()
    return enriched


# ── UBICACION_UNICA — single-location case ───────────────────────────────────

@pytest.mark.asyncio
async def test_no_branches_injects_ubicacion_unica():
    """Matriz with empty branches list → [UBICACION_UNICA] hint must appear."""
    enriched = await _call_builder(
        restaurant_obj=_matriz(),
        table_context=None,
        branches_return=[],
    )
    assert "[UBICACION_UNICA:" in enriched
    assert "UNA sola sede" in enriched
    assert "NUNCA preguntes" in enriched
    assert "[SUCURSALES:" not in enriched, "Should not inject SUCURSALES when there are none"


# ── BRANCHES — multi-location case ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_branches_present_injects_branches_block():
    """Matriz with branches → [SUCURSALES: ...] block, no UBICACION_UNICA."""
    branches = [
        {"id": 11, "name": "Sede Norte",   "address": "Cra 7 #100"},
        {"id": 12, "name": "Sede Centro",  "address": "Cl 12 #5"},
    ]
    enriched = await _call_builder(
        restaurant_obj=_matriz(),
        table_context=None,
        branches_return=branches,
    )
    assert "[SUCURSALES:" in enriched
    assert "ID:11 Sede Norte" in enriched
    assert "ID:12 Sede Centro" in enriched
    assert "[UBICACION_UNICA:" not in enriched, "Must not double-inject when branches exist"


@pytest.mark.asyncio
async def test_branch_without_address_has_fallback_text():
    """Branches with no address must render 'sin dirección' (legacy contract)."""
    branches = [{"id": 99, "name": "Sede X"}]  # no address key
    enriched = await _call_builder(
        restaurant_obj=_matriz(),
        table_context=None,
        branches_return=branches,
    )
    assert "ID:99 Sede X — sin dirección" in enriched


# ── Negative cases — neither block injected ──────────────────────────────────

@pytest.mark.asyncio
async def test_branch_restaurant_skips_branches_block():
    """When the restaurant is itself a branch (has parent), neither hint fires."""
    enriched = await _call_builder(
        restaurant_obj=_branch(rid=2, parent=1),
        table_context=None,
        branches_return=[],   # would not even be queried, but mocked anyway
    )
    assert "[UBICACION_UNICA:" not in enriched
    assert "[SUCURSALES:" not in enriched


@pytest.mark.asyncio
async def test_table_context_skips_branches_block():
    """Dine-in flow (table_context present) does not need branch hints."""
    enriched = await _call_builder(
        restaurant_obj=_matriz(),
        table_context={"id": "tbl-1", "name": "Mesa 5"},
        branches_return=[],
    )
    assert "[UBICACION_UNICA:" not in enriched
    assert "[SUCURSALES:" not in enriched
    assert "[MESA: Mesa 5]" in enriched, "Dine-in should still show table note"


# ── Resilience: db error in branches lookup must not crash ───────────────────

@pytest.mark.asyncio
async def test_branches_db_error_does_not_crash_builder():
    """If db_get_branches throws, the builder logs and continues with no hint."""
    from app.services import agent
    patches = [
        patch.object(agent.db, "db_get_history", AsyncMock(return_value=[])),
        patch.object(agent.orders, "cart_summary", AsyncMock(return_value="")),
        patch.object(agent.db, "db_get_menu_availability", AsyncMock(return_value={})),
        patch.object(agent.db, "db_get_menu", AsyncMock(return_value={"Pizzas": [{"name": "X", "price": 1}]})),
        patch.object(agent.db, "db_get_branches", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(agent, "_tenant_conn", _fake_tenant_conn),
    ]
    for p in patches:
        p.start()
    try:
        from app.services.agent import _build_enriched_user_message
        from app.services.tenant_context import bypass_tenant_scope
        with bypass_tenant_scope("test_branches_hint_unit_no_real_db"):
            enriched, _menu_url, _hist = await _build_enriched_user_message(
                user_message_clean="hola",
                user_phone="+573001234567",
                bot_number="573001112222",
                restaurant_obj=_matriz(),
                restaurant_name="Test",
                feats={},
                payment_methods_text="",
                table_context=None,
                session_state={},
            )
    finally:
        for p in patches:
            p.stop()
    # No hint either way, but the message must exist
    assert "[UBICACION_UNICA:" not in enriched
    assert "[SUCURSALES:" not in enriched
    assert "[RESTAURANTE: Test]" in enriched
