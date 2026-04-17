"""
tests/test_wompi_idempotency.py

Unit tests for Wompi webhook idempotency (Phase 2.2).

Covers:
  1. record_wompi_event returns True on first call (new event_id).
  2. record_wompi_event returns False on subsequent calls (replay).
  3. Different event_ids each return True independently.
  4. Full handler: sending the same callback body twice does NOT call
     db_confirm_payment a second time (loyalty / notification not doubled).
  5. Missing event_id in payload → 400, record_wompi_event never called.

No live DB required — pool / connection are fully mocked.
"""
from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from fastapi.testclient import TestClient

from app.main import app


# ── Helpers ───────────────────────────────────────────────────────────────────

_SECRET = "test_wompi_secret_abc123"


def _make_wompi_payload(
    event_id: str = "txn-uuid-001",
    status: str = "APPROVED",
    reference: str = "order-ref-42",
) -> dict:
    return {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                "id": event_id,
                "status": status,
                "reference": reference,
            }
        },
    }


def _wompi_checksum(body_bytes: bytes, secret: str) -> str:
    """Reproduce the HMAC the handler verifies."""
    return hashlib.sha256((body_bytes.decode() + secret).encode()).hexdigest()


def _signed_request(client: TestClient, payload: dict, secret: str = _SECRET):
    body_bytes = json.dumps(payload).encode()
    checksum = _wompi_checksum(body_bytes, secret)
    return client.post(
        "/api/payment/wompi-webhook",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "x-event-checksum": checksum,
        },
    )


# ── 1. record_wompi_event — first call returns True ──────────────────────────

@pytest.mark.asyncio
async def test_record_wompi_event_first_call_returns_true():
    """INSERT succeeds → row count 1 → True."""
    from app.repositories.orders_repo import record_wompi_event

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        result = await record_wompi_event(
            "evt-abc",
            transaction_id="evt-abc",
            order_id=None,
            status="APPROVED",
        )

    assert result is True
    conn.execute.assert_awaited_once()


# ── 2. record_wompi_event — duplicate call returns False ─────────────────────

@pytest.mark.asyncio
async def test_record_wompi_event_duplicate_returns_false():
    """ON CONFLICT DO NOTHING → row count 0 → False."""
    from app.repositories.orders_repo import record_wompi_event

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 0")

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        result = await record_wompi_event(
            "evt-abc",
            transaction_id="evt-abc",
            order_id=None,
            status="APPROVED",
        )

    assert result is False


# ── 3. Different event_ids both succeed ───────────────────────────────────────

@pytest.mark.asyncio
async def test_record_wompi_event_different_ids_both_true():
    """Two distinct event_ids each see INSERT 0 1 → both True."""
    from app.repositories.orders_repo import record_wompi_event

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("app.services.database.get_pool", AsyncMock(return_value=pool)):
        r1 = await record_wompi_event(
            "evt-001", transaction_id="evt-001", order_id=None, status="APPROVED"
        )
        r2 = await record_wompi_event(
            "evt-002", transaction_id="evt-002", order_id=None, status="DECLINED"
        )

    assert r1 is True
    assert r2 is True
    assert conn.execute.await_count == 2


# ── 4. Full handler: second identical callback skips db_confirm_payment ───────

def test_wompi_handler_dedup_skips_second_processing():
    """
    Sending the same Wompi callback twice:
    - First request  → record_wompi_event returns True → db_confirm_payment called once.
    - Second request → record_wompi_event returns False → db_confirm_payment NOT called again.
    """
    confirm_mock = AsyncMock(return_value={
        "restaurant_id": 1,
        "phone": "573000000001",
        "total": 50000,
        "bot_number": "573100000001",
    })

    # Sequence: first call True (new), second call False (replay)
    record_mock = AsyncMock(side_effect=[True, False])

    with (
        # Patch the module-level constant (read at import time, not via os.getenv at runtime)
        patch("app.routes.orders_routes.WOMPI_EVENTS_SECRET", _SECRET),
        patch("app.routes.orders_routes.record_wompi_event", record_mock),
        patch("app.services.database.db_confirm_payment", confirm_mock),
        # Suppress loyalty side effects
        patch("app.services.database.db_get_restaurant_by_bot_number", AsyncMock(return_value=None)),
    ):
        client = TestClient(app)
        payload = _make_wompi_payload(event_id="txn-idempotent-777", status="APPROVED")

        resp1 = _signed_request(client, payload)
        resp2 = _signed_request(client, payload)

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # db_confirm_payment must have been called exactly once
    confirm_mock.assert_awaited_once()

    # Second response signals replay
    assert resp2.json().get("status") == "already_processed"


# ── 5. Missing event_id → 400, record_wompi_event never called ───────────────

def test_wompi_handler_missing_event_id_returns_400():
    """If data.transaction.id is absent from the payload we return 400."""
    record_mock = AsyncMock(return_value=True)

    payload = {
        "event": "transaction.updated",
        "data": {
            "transaction": {
                # No "id" field
                "status": "APPROVED",
                "reference": "some-ref",
            }
        },
    }

    with (
        patch("app.routes.orders_routes.WOMPI_EVENTS_SECRET", _SECRET),
        patch("app.routes.orders_routes.record_wompi_event", record_mock),
    ):
        client = TestClient(app)
        resp = _signed_request(client, payload)

    assert resp.status_code == 400
    record_mock.assert_not_awaited()
