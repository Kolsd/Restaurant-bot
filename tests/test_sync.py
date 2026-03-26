"""
Suite 5 — Offline Sync Engine: POST /api/sync
tests/test_sync.py

Covers:
  1. Empty batch → 200, synced=0, no errors
  2. Batch with known type (staff_shift) → synced=N
  3. Batch with unknown type → partial error (error entry per unknown op)
  4. Idempotence: identical batch sent twice → second send is a silent upsert,
     synced count same, no duplicate errors
  5. No auth → 401
  6. Malformed payload (missing required field) → 422
  7. db_sync_batch dispatches to registered handler
  8. db_sync_batch unknown type marks op as error, not exception
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from tests.conftest import make_pool, patch_auth


_HEADERS = {"Authorization": "Bearer tok"}


# ── Shared auth + restaurant ─────────────────────────────────────────────────

def _auth(monkeypatch):
    return patch_auth(monkeypatch, features={})


# ══════════════════════════════════════════════════════════════════════════════
# HTTP endpoint tests
# ══════════════════════════════════════════════════════════════════════════════

def test_sync_empty_batch(client, monkeypatch):
    """Empty operations array → 200, synced=0, errors=[]."""
    _auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_sync_batch", AsyncMock(return_value=[]))

    r = client.post("/api/sync", json={"operations": []}, headers=_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["synced"] == 0
    assert data["errors"] == []
    assert data["total"] == 0


def test_sync_known_operations(client, monkeypatch):
    """Batch of known-type ops → synced count equals batch size."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    ops = [
        {"id": "aaaaaaaa-0000-4000-8000-000000000001", "type": "staff_shift",
         "action": "upsert", "data": {"staff_id": "s1", "restaurant_id": 1},
         "client_ts": "2026-03-25T10:00:00Z"},
        {"id": "aaaaaaaa-0000-4000-8000-000000000002", "type": "staff_shift",
         "action": "upsert", "data": {"staff_id": "s2", "restaurant_id": 1},
         "client_ts": "2026-03-25T10:01:00Z"},
    ]

    monkeypatch.setattr(db_mod, "db_sync_batch", AsyncMock(return_value=[
        {"id": ops[0]["id"], "status": "ok"},
        {"id": ops[1]["id"], "status": "ok"},
    ]))

    r = client.post("/api/sync", json={"operations": ops}, headers=_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["synced"] == 2
    assert data["errors"] == []
    assert data["total"] == 2


def test_sync_unknown_type_returns_partial_error(client, monkeypatch):
    """Unknown operation type → that op appears in errors, known ops succeed."""
    _auth(monkeypatch)
    import app.services.database as db_mod

    ops = [
        {"id": "aaaaaaaa-0000-4000-8000-000000000001", "type": "staff_shift",
         "action": "upsert", "data": {}, "client_ts": None},
        {"id": "aaaaaaaa-0000-4000-8000-000000000099", "type": "unknown_entity",
         "action": "upsert", "data": {}, "client_ts": None},
    ]

    monkeypatch.setattr(db_mod, "db_sync_batch", AsyncMock(return_value=[
        {"id": ops[0]["id"], "status": "ok"},
        {"id": ops[1]["id"], "status": "error", "error": "Unknown type: unknown_entity"},
    ]))

    r = client.post("/api/sync", json={"operations": ops}, headers=_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["synced"] == 1
    assert len(data["errors"]) == 1
    assert data["errors"][0]["id"] == ops[1]["id"]


def test_sync_requires_auth(client, monkeypatch):
    """Request without valid token → 401."""
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))

    r = client.post("/api/sync",
                    json={"operations": []},
                    headers={"Authorization": "Bearer invalid"})
    assert r.status_code == 401


def test_sync_malformed_payload_422(client, monkeypatch):
    """Missing required 'id' field in an operation → 422."""
    _auth(monkeypatch)

    r = client.post(
        "/api/sync",
        json={"operations": [{"type": "staff_shift", "action": "upsert", "data": {}}]},
        headers=_HEADERS,
    )
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Idempotence test
# ══════════════════════════════════════════════════════════════════════════════

def test_sync_idempotence(client, monkeypatch):
    """
    Sending the exact same batch twice must yield the same result both times.
    The second send must not raise an error — it's a silent upsert.
    """
    _auth(monkeypatch)
    import app.services.database as db_mod

    op = {
        "id":        "aaaaaaaa-0000-4000-8000-000000000001",
        "type":      "staff_shift",
        "action":    "upsert",
        "data":      {"staff_id": "s1", "restaurant_id": 1},
        "client_ts": "2026-03-25T10:00:00Z",
    }
    ok_result = [{"id": op["id"], "status": "ok"}]

    # Both calls return the same "ok" result — simulating ON CONFLICT DO UPDATE
    monkeypatch.setattr(db_mod, "db_sync_batch",
                        AsyncMock(side_effect=[ok_result, ok_result]))

    r1 = client.post("/api/sync", json={"operations": [op]}, headers=_HEADERS)
    r2 = client.post("/api/sync", json={"operations": [op]}, headers=_HEADERS)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["synced"] == r2.json()["synced"] == 1
    assert r1.json()["errors"] == r2.json()["errors"] == []


# ══════════════════════════════════════════════════════════════════════════════
# DB layer unit tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_db_sync_batch_dispatches_registered_handler():
    """
    db_sync_batch must call the registered handler for a known type.
    We register a test handler and verify it is invoked.
    """
    from app.services import database as db

    invocations = []

    # Register a temporary test handler
    original_handlers = dict(db._SYNC_HANDLERS)
    try:
        @db._register_sync_handler("test_entity")
        async def _test_handler(conn, restaurant_id, data):
            invocations.append((restaurant_id, data))

        mock_conn = AsyncMock()
        # conn.transaction() is used as `async with conn.transaction():`
        # AsyncMock's default __aenter__/__aexit__ are coroutines — this works.
        mock_conn.transaction = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=False),
        ))
        mock_pool = make_pool(mock_conn)

        with patch.object(db, "get_pool", AsyncMock(return_value=mock_pool)):
            results = await db.db_sync_batch(
                restaurant_id=1,
                operations=[{
                    "id":     "ffffffff-0000-4000-8000-000000000001",
                    "type":   "test_entity",
                    "action": "upsert",
                    "data":   {"key": "value"},
                }],
            )
    finally:
        # Restore original handler registry
        db._SYNC_HANDLERS.clear()
        db._SYNC_HANDLERS.update(original_handlers)

    assert len(results) == 1
    assert results[0]["status"] == "ok"
    assert len(invocations) == 1
    assert invocations[0][0] == 1       # restaurant_id
    assert invocations[0][1]["key"] == "value"


@pytest.mark.asyncio
async def test_db_sync_batch_unknown_type_returns_error_not_exception():
    """
    Passing an op with an unknown type must return status='error' in the result,
    not raise an exception that would fail the entire batch.
    """
    from app.services import database as db

    mock_conn = AsyncMock()
    mock_pool = make_pool(mock_conn)

    with patch.object(db, "get_pool", AsyncMock(return_value=mock_pool)):
        results = await db.db_sync_batch(
            restaurant_id=1,
            operations=[{
                "id":     "eeeeeeee-0000-4000-8000-000000000001",
                "type":   "absolutely_unknown_type_xyz",
                "action": "upsert",
                "data":   {},
            }],
        )

    assert len(results) == 1
    # db_sync_batch uses status "unsupported_type" for unregistered handlers
    assert results[0]["status"] == "unsupported_type"
    assert "unknown" in results[0]["error"].lower() or "No sync handler" in results[0]["error"]
