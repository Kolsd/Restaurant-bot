"""
tests/test_demo_routes.py
=========================
Unit tests for the /api/demo/* endpoints.

Strategy: mock agent.chat + Redis session helpers so tests run without real DB
or LLM. Six tests covering the contract from the task spec.

Tests:
  1. POST /api/demo/start  returns valid session_id + customer_phone format
  2. POST /api/demo/chat   with invalid session_id returns 404
  3. POST /api/demo/chat   increments message_count
  4. POST /api/demo/chat   after 30 messages returns expired flag without calling bot
  5. POST /api/demo/reset  clears the session
  6. Rate limit on /api/demo/start triggers after 100 requests/hour from same IP

Uses the conftest TestClient (no lifespan startup — no real DB needed).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# conftest.py creates `client` fixture from app without lifespan

FAKE_ORG_ID = 42
FAKE_AGENT_REPLY = "Hola, bienvenido a Demo Mesio. ¿Qué deseas ordenar?"


def _make_session(message_count: int = 0) -> dict:
    return {
        "customer_phone": f"demo:{uuid.uuid4()}",
        "restaurant_id": FAKE_ORG_ID,
        "message_count": message_count,
        "created_at": "2026-05-05T10:00:00+00:00",
    }


@contextmanager
def _mock_tenant_scope(org_id: int = FAKE_ORG_ID):
    """Return a no-op context manager patching tenant_scope in the demo route."""
    mock_cm = MagicMock()
    mock_cm.__enter__ = MagicMock(return_value=org_id)
    mock_cm.__exit__ = MagicMock(return_value=False)
    with patch("app.routes.demo.tenant_scope", return_value=mock_cm):
        yield


# ── Test 1: /start returns valid shape ────────────────────────────────────────

def test_start_returns_valid_session(client):
    """POST /api/demo/start returns session_id + customer_phone with demo: prefix."""
    fake_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    with (
        patch("app.routes.demo.state_store.rate_limit_check",
              new_callable=AsyncMock, return_value=True),
        patch("app.repositories.demo_repo.db_get_demo_org_id",
              new_callable=AsyncMock, return_value=FAKE_ORG_ID),
        patch("app.routes.demo._save_session", new_callable=AsyncMock) as mock_save,
        patch("app.routes.demo.uuid.uuid4", return_value=uuid.UUID(fake_uuid)),
    ):
        resp = client.post("/api/demo/start", json={})

    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["session_id"] == fake_uuid
    assert data["customer_phone"] == f"demo:{fake_uuid}"
    assert data["restaurant_id"] == FAKE_ORG_ID
    assert data["restaurant_name"] == "Demo Mesio"

    assert mock_save.called
    saved_data = mock_save.call_args[0][1]
    assert saved_data["restaurant_id"] == FAKE_ORG_ID
    assert saved_data["message_count"] == 0


# ── Test 2: /chat with unknown session returns 404 ────────────────────────────

def test_chat_unknown_session_returns_404(client):
    """POST /api/demo/chat with a session_id not in Redis returns 404."""
    with patch("app.routes.demo._get_session", new_callable=AsyncMock, return_value=None):
        resp = client.post("/api/demo/chat", json={
            "session_id": "does-not-exist",
            "message": "Hola",
        })

    assert resp.status_code == 404


# ── Test 3: /chat increments message_count ────────────────────────────────────

def test_chat_increments_message_count(client):
    """POST /api/demo/chat returns message_count = previous + 1."""
    session = _make_session(message_count=5)
    mock_result = {"message": FAKE_AGENT_REPLY}

    with (
        patch("app.routes.demo._get_session",
              new_callable=AsyncMock, return_value=session),
        patch("app.routes.demo._save_session", new_callable=AsyncMock),
        patch("app.services.agent.chat",
              new_callable=AsyncMock, return_value=mock_result),
    ):
        with _mock_tenant_scope():
            resp = client.post("/api/demo/chat", json={
                "session_id": "test-session-id",
                "message": "Quiero una bandeja paisa",
            })

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["message_count"] == 6
    assert data["max_messages"] == 30
    assert data["expired"] is False


# ── Test 4: /chat at cap returns expired flag without calling bot ──────────────

def test_chat_at_cap_returns_expired_without_bot(client):
    """When message_count >= 30, /chat returns expired:True and never calls agent."""
    session = _make_session(message_count=30)

    with (
        patch("app.routes.demo._get_session",
              new_callable=AsyncMock, return_value=session),
        patch("app.services.agent.chat", new_callable=AsyncMock) as mock_agent,
    ):
        resp = client.post("/api/demo/chat", json={
            "session_id": "capped-session",
            "message": "Quiero algo más",
        })

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["expired"] is True
    assert data["message_count"] == 30
    mock_agent.assert_not_called()
    assert "30" in data["reply"] or "Reiniciar" in data["reply"]


# ── Test 5: /reset clears the session ────────────────────────────────────────

def test_reset_clears_session(client):
    """POST /api/demo/reset deletes the Redis key."""
    session = _make_session(message_count=10)

    with (
        patch("app.routes.demo._get_session",
              new_callable=AsyncMock, return_value=session),
        patch("app.routes.demo._delete_session",
              new_callable=AsyncMock) as mock_delete,
        patch("app.repositories.demo_repo.db_cleanup_demo_session",
              new_callable=AsyncMock),
    ):
        with _mock_tenant_scope():
            resp = client.post("/api/demo/reset", json={"session_id": "to-reset"})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    mock_delete.assert_called_once_with("to-reset")


# ── Test 6: rate limit on /start triggers ────────────────────────────────────

def test_start_rate_limit_triggers(client):
    """POST /api/demo/start returns 429 when rate_limit_check returns False."""
    with patch("app.routes.demo.state_store.rate_limit_check",
               new_callable=AsyncMock, return_value=False):
        resp = client.post("/api/demo/start", json={})

    assert resp.status_code == 429
    assert "detail" in resp.json()
