"""
tests/test_resolve_location_id.py

Unit tests for _resolve_location_id in agent.py:
  - Priority: incoming_location_id > table_context > routing_context > conversation DB > None
  - Backward-compat alias _resolve_branch_id returns same value
  - Legacy branch_id keys in table_context / routing_context are also accepted

Tests:
  1. test_resolve_returns_table_context_location_first
  2. test_resolve_falls_back_to_routing_context
  3. test_resolve_falls_back_to_conversation_state
  4. test_resolve_returns_none_when_nothing_known
  5. test_resolve_incoming_takes_priority_over_table_context
  6. test_resolve_branch_id_alias_returns_same_value
  7. test_resolve_accepts_legacy_branch_id_in_table_context
  8. test_resolve_accepts_legacy_branch_id_in_routing_context
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch


# ── Test 1: table_context["location_id"] is highest priority (after incoming) ─

@pytest.mark.asyncio
async def test_resolve_returns_table_context_location_first():
    """location_id in table_context should win over routing_context and conversation."""
    from app.services.agent import _resolve_location_id

    table_ctx = {"location_id": 10, "name": "Mesa 3"}
    routing_ctx = {"location_id": 99}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=50),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 10


# ── Test 2: routing_context fallback ────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_falls_back_to_routing_context():
    """When table_context has no location, routing_context is used."""
    from app.services.agent import _resolve_location_id

    table_ctx = {"name": "Mesa 3"}  # no location_id
    routing_ctx = {"location_id": 20}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=50),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 20


# ── Test 3: conversation state fallback ────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_falls_back_to_conversation_state():
    """When no table/routing context, last known location from conversation is used."""
    from app.services.agent import _resolve_location_id

    table_ctx = None
    routing_ctx = {}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=33),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 33


# ── Test 4: returns None when nothing is known ────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_known():
    """When all sources return None/empty, result is None (exploratory chat)."""
    from app.services.agent import _resolve_location_id

    table_ctx = None
    routing_ctx = {}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=None),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result is None


# ── Test 5: incoming_location_id takes absolute priority ─────────────────────

@pytest.mark.asyncio
async def test_resolve_incoming_takes_priority_over_table_context():
    """incoming_location_id from inbox_worker beats table_context."""
    from app.services.agent import _resolve_location_id

    table_ctx = {"location_id": 10}
    routing_ctx = {"location_id": 20}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=30),
    ):
        result = await _resolve_location_id(
            table_ctx, routing_ctx, "5551234", "bot123",
            incoming_location_id=5,
        )

    assert result == 5


# ── Test 6: _resolve_branch_id alias returns identical value ─────────────────

@pytest.mark.asyncio
async def test_resolve_branch_id_alias_returns_same_value():
    """_resolve_branch_id is a backward-compat alias for _resolve_location_id."""
    from app.services.agent import _resolve_branch_id

    table_ctx = {"branch_id": 77}
    routing_ctx = {}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=None),
    ):
        result = await _resolve_branch_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 77


# ── Test 7: legacy branch_id in table_context is accepted ────────────────────

@pytest.mark.asyncio
async def test_resolve_accepts_legacy_branch_id_in_table_context():
    """table_context["branch_id"] (legacy key) is accepted when location_id is absent."""
    from app.services.agent import _resolve_location_id

    table_ctx = {"name": "Mesa 5", "branch_id": 55}  # legacy key
    routing_ctx = {}

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=None),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 55


# ── Test 8: legacy branch_id in routing_context is accepted ──────────────────

@pytest.mark.asyncio
async def test_resolve_accepts_legacy_branch_id_in_routing_context():
    """routing_context["branch_id"] (legacy key) is accepted when location_id is absent."""
    from app.services.agent import _resolve_location_id

    table_ctx = None
    routing_ctx = {"branch_id": 88}  # legacy key, set by agent_external GPS routing

    with patch(
        "app.repositories.conversations_repo.db_get_conversation_location_id",
        AsyncMock(return_value=None),
    ):
        result = await _resolve_location_id(table_ctx, routing_ctx, "5551234", "bot123")

    assert result == 88
