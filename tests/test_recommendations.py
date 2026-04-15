"""
Suite — Personalized Recommendations from Order History (Fase 5a)
tests/test_recommendations.py

Tests:
  1. db_get_customer_order_history returns [] for a customer with no orders
  2. db_get_customer_order_history returns [] for a customer with 1 order (< 2 threshold)
  3. db_get_customer_order_history returns aggregated top dishes for a customer with 3 orders
  4. build_system_prompt includes <customer_history> block when order_history has >= 2 items
  5. build_system_prompt omits <customer_history> block when order_history is empty
  6. build_system_prompt omits <customer_history> block when order_history has only 1 item

All tests are fully mocked — no real DB is touched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import make_pool, make_row
from unittest.mock import patch as _patch
from app.services.tenant_context import tenant_scope


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_conn_for_history(fetchval_result: int, fetch_result: list):
    """Mock DB connection with fetchval (order count) + fetch (aggregated rows)."""
    conn = MagicMock()
    # tenant_connection() calls conn.fetchval("SELECT set_config(...)") first,
    # then the repo calls conn.fetchval for the order count, then conn.fetch.
    conn.fetchval = AsyncMock(side_effect=[None, fetchval_result])
    conn.fetch = AsyncMock(return_value=fetch_result)
    txn = MagicMock()
    txn.__aenter__ = AsyncMock(return_value=txn)
    txn.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_history_row(name: str, count: int, days_ago: int = 5):
    """Create a mock asyncpg Record for an aggregated order-history row."""
    last_ordered = datetime(2026, 4, 14 - days_ago, 12, 0, 0, tzinfo=timezone.utc)
    return make_row({"name": name, "count": count, "last_ordered": last_ordered})


# ══════════════════════════════════════════════════════════════════════════════
# Class: TestDbGetCustomerOrderHistory
# ══════════════════════════════════════════════════════════════════════════════

class TestDbGetCustomerOrderHistory:

    # ── 1. Customer with zero orders → [] ────────────────────────────────────

    def test_returns_empty_for_customer_with_no_orders(self, monkeypatch):
        conn = _make_conn_for_history(fetchval_result=0, fetch_result=[])
        from app.repositories.conversations_repo import db_get_customer_order_history
        with _patch("app.services.database.get_pool", AsyncMock(return_value=make_pool(conn))):
            with tenant_scope(1):
                result = _run(db_get_customer_order_history(phone="+57300", restaurant_id=1))
        assert result == []
        # fetch() must NOT have been called (early return after fetchval < 2)
        conn.fetch.assert_not_awaited()

    # ── 2. Customer with exactly 1 order → [] (below recurrent threshold) ────

    def test_returns_empty_for_single_order_customer(self, monkeypatch):
        conn = _make_conn_for_history(fetchval_result=1, fetch_result=[])
        from app.repositories.conversations_repo import db_get_customer_order_history
        with _patch("app.services.database.get_pool", AsyncMock(return_value=make_pool(conn))):
            with tenant_scope(1):
                result = _run(db_get_customer_order_history(phone="+57300", restaurant_id=1))
        assert result == []
        conn.fetch.assert_not_awaited()

    # ── 3. Customer with 3 orders of 2 dishes → correct aggregation ──────────

    def test_returns_aggregated_top_dishes_for_recurrent_customer(self, monkeypatch):
        # Simulate DB returning 2 aggregated rows
        db_rows = [
            _make_history_row("Bandeja Paisa", count=3, days_ago=4),
            _make_history_row("Limonada de Coco", count=2, days_ago=6),
        ]
        conn = _make_conn_for_history(fetchval_result=3, fetch_result=db_rows)
        from app.repositories.conversations_repo import db_get_customer_order_history
        with _patch("app.services.database.get_pool", AsyncMock(return_value=make_pool(conn))):
            with tenant_scope(1):
                result = _run(db_get_customer_order_history(phone="+57300", restaurant_id=1))

        assert len(result) == 2
        # First item: most frequent
        assert result[0]["name"] == "Bandeja Paisa"
        assert result[0]["count"] == 3
        assert "2026-04-10" in result[0]["last_ordered"]  # days_ago=4 → 2026-04-10
        # Second item
        assert result[1]["name"] == "Limonada de Coco"
        assert result[1]["count"] == 2
        # fetch() was called exactly once for the aggregation query
        conn.fetch.assert_awaited_once()

    # ── 4. Limit is capped at 3 max ───────────────────────────────────────────

    def test_limit_is_capped_at_three(self, monkeypatch):
        db_rows = [
            _make_history_row("Dish A", count=5, days_ago=1),
            _make_history_row("Dish B", count=4, days_ago=2),
            _make_history_row("Dish C", count=3, days_ago=3),
        ]
        conn = _make_conn_for_history(fetchval_result=10, fetch_result=db_rows)
        from app.repositories.conversations_repo import db_get_customer_order_history
        with _patch("app.services.database.get_pool", AsyncMock(return_value=make_pool(conn))):
            with tenant_scope(1):
                # Even if caller passes limit=10, the query caps at 3
                result = _run(db_get_customer_order_history(phone="+57300", restaurant_id=1, limit=10))
        # The mock returns exactly 3 rows — function must not add or remove any
        assert len(result) == 3
        # Verify that the fetch was called with limit=3 (min(10, 3))
        call_args = conn.fetch.call_args
        # Third positional arg to the query is the limit value
        assert call_args[0][3] == 3


# ══════════════════════════════════════════════════════════════════════════════
# Class: TestBuildSystemPromptHistoryBlock
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildSystemPromptHistoryBlock:

    # ── 5. <customer_history> block appears when order_history has >= 2 items ──

    def test_history_block_injected_for_recurrent_customer(self):
        from app.services.agent import build_system_prompt

        order_history = [
            {"name": "Bandeja Paisa", "count": 3, "last_ordered": "2026-04-10T14:00:00"},
            {"name": "Limonada de Coco", "count": 2, "last_ordered": "2026-04-08T12:00:00"},
        ]

        with patch(
            "app.repositories.discounts_repo.db_get_active_discount",
            new=AsyncMock(return_value=None),
        ):
            prompt = _run(
                build_system_prompt(
                    features={},
                    table_context=None,
                    restaurant_id=None,
                    customer_context="",
                    order_history=order_history,
                )
            )

        full_text = " ".join(
            block["text"] for block in prompt if isinstance(block, dict) and block.get("type") == "text"
        )
        assert "<customer_history" in full_text
        assert 'source="internal"' in full_text
        assert 'trust="trusted"' in full_text
        assert "Bandeja Paisa (×3)" in full_text
        assert "Limonada de Coco (×2)" in full_text
        assert "lo de siempre" in full_text

    # ── 6. No block when order_history is empty ───────────────────────────────

    def test_history_block_absent_for_no_history(self):
        from app.services.agent import build_system_prompt

        with patch(
            "app.repositories.discounts_repo.db_get_active_discount",
            new=AsyncMock(return_value=None),
        ):
            prompt = _run(
                build_system_prompt(
                    features={},
                    table_context=None,
                    restaurant_id=None,
                    customer_context="",
                    order_history=[],
                )
            )

        full_text = " ".join(
            block["text"] for block in prompt if isinstance(block, dict) and block.get("type") == "text"
        )
        assert "<customer_history" not in full_text

    # ── 7. No block when order_history has exactly 1 item (< 2 threshold) ────

    def test_history_block_absent_for_single_history_item(self):
        from app.services.agent import build_system_prompt

        order_history = [
            {"name": "Solo un Plato", "count": 1, "last_ordered": "2026-04-10T14:00:00"},
        ]

        with patch(
            "app.repositories.discounts_repo.db_get_active_discount",
            new=AsyncMock(return_value=None),
        ):
            prompt = _run(
                build_system_prompt(
                    features={},
                    table_context=None,
                    restaurant_id=None,
                    customer_context="",
                    order_history=order_history,
                )
            )

        full_text = " ".join(
            block["text"] for block in prompt if isinstance(block, dict) and block.get("type") == "text"
        )
        assert "<customer_history" not in full_text

    # ── 8. Block does not appear when order_history is None ───────────────────

    def test_history_block_absent_when_none(self):
        from app.services.agent import build_system_prompt

        with patch(
            "app.repositories.discounts_repo.db_get_active_discount",
            new=AsyncMock(return_value=None),
        ):
            prompt = _run(
                build_system_prompt(
                    features={},
                    table_context=None,
                    restaurant_id=None,
                    customer_context="",
                    order_history=None,
                )
            )

        full_text = " ".join(
            block["text"] for block in prompt if isinstance(block, dict) and block.get("type") == "text"
        )
        assert "<customer_history" not in full_text
