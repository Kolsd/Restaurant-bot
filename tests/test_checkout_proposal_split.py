"""
tests/test_checkout_proposal_split.py
======================================
Regression tests for audit bug TOP-2:

  _save_checkout_proposal ignored state['check_amounts'] and always
  re-computed subtotal/n. When the customer said "una paga la Club ($8K),
  otra el Camarón ($45K)", the bot promised those amounts but the DB
  stored 26.5K + 26.5K. Caja saw different numbers than the bot quoted,
  leading to disputes at the till.

These tests pin the corrected behaviour permanently.
"""

import pytest
from unittest.mock import AsyncMock, patch

from app.services.agent_salon import _save_checkout_proposal


@pytest.mark.asyncio
async def test_uses_per_item_assignment_when_check_amounts_set():
    """Customer said "una paga la Club $8K, otra el Camarón $45K".
    state['check_amounts'] = ['8000', '45000'].
    The DB rows MUST be 8000 + 45000, NOT 26500 + 26500.
    """
    state = {
        "base_order_id": "ORD-AAA",
        "split_count": 2,
        "subtotal": "53000",
        "tip_amount": "0",
        "check_amounts": ["8000", "45000"],
        "payments": [[], []],
    }

    captured_payloads = {}

    async def fake_create_checks(base_order_id, checks_payload):
        captured_payloads["base_order_id"] = base_order_id
        captured_payloads["checks"] = checks_payload
        # Return the same shape db_create_checks would
        return [{"id": f"chk-{i+1}"} for i in range(len(checks_payload))]

    with patch("app.services.agent_salon.db.db_create_checks", side_effect=fake_create_checks), \
         patch("app.services.agent_salon.db.db_attach_proposal", AsyncMock()), \
         patch("app.services.agent_salon.db.db_set_check_tip", AsyncMock()):
        await _save_checkout_proposal("3001234567", "+57888", state, table_context=None)

    checks = captured_payloads["checks"]
    assert len(checks) == 2
    # Per-item assignment respected:
    assert checks[0]["total"] == 8000.0
    assert checks[1]["total"] == 45000.0
    # Subtotal column matches total (no tax in this case).
    assert checks[0]["subtotal"] == 8000.0
    assert checks[1]["subtotal"] == 45000.0


@pytest.mark.asyncio
async def test_falls_back_to_even_split_when_check_amounts_none():
    """No per-item assignment captured. Should split evenly."""
    state = {
        "base_order_id": "ORD-BBB",
        "split_count": 2,
        "subtotal": "50000",
        "tip_amount": "0",
        "check_amounts": None,
        "payments": [[], []],
    }

    captured = {}

    async def fake_create_checks(base_order_id, checks_payload):
        captured["checks"] = checks_payload
        return [{"id": f"chk-{i+1}"} for i in range(len(checks_payload))]

    with patch("app.services.agent_salon.db.db_create_checks", side_effect=fake_create_checks), \
         patch("app.services.agent_salon.db.db_attach_proposal", AsyncMock()), \
         patch("app.services.agent_salon.db.db_set_check_tip", AsyncMock()):
        await _save_checkout_proposal("3001234567", "+57888", state, table_context=None)

    checks = captured["checks"]
    assert checks[0]["total"] == 25000.0
    assert checks[1]["total"] == 25000.0


@pytest.mark.asyncio
async def test_falls_back_to_even_split_when_check_amounts_length_mismatch():
    """Defensive: if check_amounts length doesn't match split_count, ignore it
    rather than partially-fill the checks (would be a corruption vector)."""
    state = {
        "base_order_id": "ORD-CCC",
        "split_count": 3,
        "subtotal": "30000",
        "tip_amount": "0",
        "check_amounts": ["10000", "20000"],  # only 2 entries, n=3
        "payments": [[], [], []],
    }

    captured = {}

    async def fake_create_checks(base_order_id, checks_payload):
        captured["checks"] = checks_payload
        return [{"id": f"chk-{i+1}"} for i in range(len(checks_payload))]

    with patch("app.services.agent_salon.db.db_create_checks", side_effect=fake_create_checks), \
         patch("app.services.agent_salon.db.db_attach_proposal", AsyncMock()), \
         patch("app.services.agent_salon.db.db_set_check_tip", AsyncMock()):
        await _save_checkout_proposal("3001234567", "+57888", state, table_context=None)

    checks = captured["checks"]
    # Even split: 30000 / 3 = 10000 each.
    assert checks[0]["total"] == 10000.0
    assert checks[1]["total"] == 10000.0
    assert checks[2]["total"] == 10000.0


@pytest.mark.asyncio
async def test_rebalances_sub_cent_drift_onto_last_check():
    """If quantization leaves the per-check sum off by a cent vs the order
    subtotal, the last check absorbs the drift. Ensures checks always add
    up to subtotal exactly (caja reconciliation requirement)."""
    state = {
        "base_order_id": "ORD-DDD",
        "split_count": 3,
        # Subtotal that doesn't divide evenly into 3 (10000.01)
        "subtotal": "10000.01",
        "tip_amount": "0",
        "check_amounts": ["3333.33", "3333.33", "3333.33"],
        "payments": [[], [], []],
    }

    captured = {}

    async def fake_create_checks(base_order_id, checks_payload):
        captured["checks"] = checks_payload
        return [{"id": f"chk-{i+1}"} for i in range(len(checks_payload))]

    with patch("app.services.agent_salon.db.db_create_checks", side_effect=fake_create_checks), \
         patch("app.services.agent_salon.db.db_attach_proposal", AsyncMock()), \
         patch("app.services.agent_salon.db.db_set_check_tip", AsyncMock()):
        await _save_checkout_proposal("3001234567", "+57888", state, table_context=None)

    checks = captured["checks"]
    total_sum = sum(c["total"] for c in checks)
    # Sum must match subtotal exactly — caja reconciles on this.
    assert total_sum == pytest.approx(10000.01, abs=0.01)
