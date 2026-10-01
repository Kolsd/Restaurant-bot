"""
Suite — Personalized Recommendations from Order History (Fase 5a)
tests/test_recommendations.py

Tests:
  (The history itself is computed by app/services/diner_memory.py —
  tests/test_diner_memory.py.)
  4. build_system_prompt includes <customer_history> block when order_history has >= 2 items
  5. build_system_prompt omits <customer_history> block when order_history is empty
  6. build_system_prompt omits <customer_history> block when order_history has only 1 item

All tests are fully mocked — no real DB is touched.
"""
from __future__ import annotations

import asyncio


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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
