"""
tests/test_agent_location_resolution.py

Unit tests for location_id propagation through the agent pipeline (Bloque S4):
  - conversation persists location_id after resolution
  - chat() accepts location_id kwarg and threads it through

The four GPS/branch-routing tests that used to live here (delivery via GPS,
delivery out-of-coverage, pickup single/multiple Location) exercised
agent_external.execute_external_action directly — that module was deleted in
chunk 9 (docs/claude/delivery-web.md): delivery/pickup ordering moved
entirely to the web channel, and a WhatsApp customer now gets a deterministic
reply instead of an LLM tool-use loop, so there is no more branch-routing
code path to test here. The underlying GPS resolver
(restaurant_repo.db_resolve_location_by_gps) still has its own coverage in
tests/test_pickup_gps_routing.py and tests/test_org_repos.py.

Tests:
  1. test_conversation_persists_location_id_once_resolved
  2. test_chat_accepts_location_id_kwarg
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Test 1: conversation persists location_id once resolved ───────────────────

@pytest.mark.asyncio
async def test_conversation_persists_location_id_once_resolved():
    """db_save_history is called with location_id; the execute SQL includes location_id param."""
    from app.repositories.conversations_repo import db_save_history
    from app.services.tenant_context import tenant_scope

    # Build a proper asyncpg conn mock following test_loyalty_repo_tenant.py pattern
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    # transaction() must be a sync MagicMock whose __aenter__/__aexit__ are async
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)

    with (
        patch("app.services.database.get_pool", AsyncMock(return_value=pool)),
        tenant_scope(42),
    ):
        await db_save_history(
            phone="5551234",
            bot_number="bot123",
            history=[{"role": "user", "content": "hola"}],
            location_id=10,
        )

    # Verify execute was called with the location_id (10) as a positional arg
    execute_calls = conn.execute.call_args_list
    # set_config call + the INSERT call
    insert_call = next(
        (c for c in execute_calls if "INSERT INTO conversations" in str(c.args[0])),
        None,
    )
    assert insert_call is not None, "No INSERT INTO conversations execute call found"
    # positional args: (sql, phone, bot_number, history_json, branch_id, location_id)
    insert_args = insert_call.args
    assert 10 in insert_args, f"location_id=10 not found in execute args: {insert_args}"


# ── Test 2: chat() accepts location_id kwarg ─────────────────────────────────

@pytest.mark.asyncio
async def test_chat_accepts_location_id_kwarg():
    """chat() must accept location_id keyword argument without raising TypeError."""
    from app.services.agent import chat
    import inspect

    sig = inspect.signature(chat)
    assert "location_id" in sig.parameters, "chat() must accept location_id kwarg"
    assert sig.parameters["location_id"].default is None, "location_id must default to None"
