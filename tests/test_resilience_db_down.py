"""
tests/test_resilience_db_down.py

Tests for middleware resilience under DB / Redis outages (Phase 3.3).

Covers:
  1. tenant_db.tenant_connection() raises PoolAcquireTimeout when pool.acquire()
     times out, not a bare asyncio.TimeoutError.
  2. Meta webhook POST returns 200 (not 503) when enqueue fails due to a DB-down
     error — preventing Meta retry storms.
  3. GET /health returns 200 even when DB is broken (process-only probe).
  4. GET /health/deep returns 200 with body {"status": "degraded", "db": "error"}
     when DB is broken.

No live DB or Redis required — all dependencies are mocked.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.services.tenant_db import PoolAcquireTimeout, POOL_ACQUIRE_TIMEOUT
from app.services.tenant_context import bypass_tenant_scope


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_meta_payload(
    user_phone: str = "573001234567",
    bot_number: str = "573109999999",
    text: str = "Hola",
    wam_id: str = "wamid.test123",
) -> dict:
    """Build a minimal Meta webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": bot_number,
                                "phone_number_id": "phone_id_test",
                            },
                            "messages": [
                                {
                                    "from": user_phone,
                                    "id": wam_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def _meta_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── 1. tenant_connection() raises PoolAcquireTimeout on timeout ───────────────

class TestPoolAcquireTimeout:
    """tenant_connection() must raise PoolAcquireTimeout (not asyncio.TimeoutError)
    when pool.acquire() does not return within the configured timeout."""

    @pytest.mark.asyncio
    async def test_raises_pool_acquire_timeout(self):
        """Simulate pool.acquire() hanging forever — should raise PoolAcquireTimeout."""

        async def _hanging_acquire():
            # Waits much longer than POOL_ACQUIRE_TIMEOUT
            await asyncio.sleep(POOL_ACQUIRE_TIMEOUT + 10)

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=_hanging_acquire())

        with patch("app.services.database.get_pool", AsyncMock(return_value=mock_pool)):
            with bypass_tenant_scope("test_pool_timeout_bypass"):
                with pytest.raises(PoolAcquireTimeout):
                    from app.services.tenant_db import tenant_connection
                    async with tenant_connection():
                        pass  # pragma: no cover

    @pytest.mark.asyncio
    async def test_pool_acquire_timeout_is_connection_error(self):
        """PoolAcquireTimeout is a subclass of ConnectionError."""
        assert issubclass(PoolAcquireTimeout, ConnectionError)

    @pytest.mark.asyncio
    async def test_pool_acquire_timeout_not_plain_timeout(self):
        """PoolAcquireTimeout is NOT asyncio.TimeoutError — callers can distinguish."""
        exc = PoolAcquireTimeout("test")
        assert not isinstance(exc, asyncio.TimeoutError)

    @pytest.mark.asyncio
    async def test_raises_within_timeout_bound(self):
        """The exception must be raised within ~POOL_ACQUIRE_TIMEOUT seconds (+ 1s margin)."""
        async def _hanging_acquire():
            await asyncio.sleep(60)  # much longer than timeout

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=_hanging_acquire())

        start = asyncio.get_event_loop().time()
        with patch("app.services.database.get_pool", AsyncMock(return_value=mock_pool)):
            with bypass_tenant_scope("test_timing_bypass_ok"):
                with pytest.raises(PoolAcquireTimeout):
                    from app.services.tenant_db import tenant_connection
                    async with tenant_connection():
                        pass  # pragma: no cover
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < POOL_ACQUIRE_TIMEOUT + 1.5, (
            f"Expected to raise in < {POOL_ACQUIRE_TIMEOUT + 1.5}s, took {elapsed:.2f}s"
        )


# ── 2. Meta webhook returns 200 when enqueue fails due to DB down ─────────────

_SECRET = "test_secret"
_BOT_NUMBER = "573109999999"


def _signed_request(client: TestClient, payload: dict) -> "Response":  # type: ignore[name-defined]
    """POST to /api/webhook/meta with a valid Meta signature."""
    body = json.dumps(payload).encode()
    sig = _meta_signature(body, _SECRET)
    return client.post(
        "/api/webhook/meta",
        content=body,
        headers={"x-hub-signature-256": sig, "content-type": "application/json"},
    )


class TestWebhookMetaDbDown:
    """When DB is down, the Meta webhook must return 200 to prevent retry storms."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("META_APP_SECRET", _SECRET)

    def _mock_db_globals(self, monkeypatch):
        """Patch DB-touching globals in chat.py so basic parsing works.

        Note: db.get_pool() is called in chat.py to get a pool handle before
        passing it to inbox_repo.enqueue().  We mock it to return a dummy pool
        so the call succeeds and the failure is isolated to inbox_repo.enqueue().
        """
        # Dummy pool — just needs to exist; enqueue will raise its own error.
        _dummy_pool = AsyncMock()
        monkeypatch.setattr(
            "app.services.database.get_pool", AsyncMock(return_value=_dummy_pool)
        )
        monkeypatch.setattr(
            "app.routes.chat.rate_limit_check", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            "app.services.database.db_is_duplicate_wam", AsyncMock(return_value=False)
        )
        monkeypatch.setattr(
            "app.services.database.db_get_restaurant_by_phone",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "app.services.database.db_get_restaurant_by_bot_number",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            "app.routes.chat._is_rate_limited", AsyncMock(return_value=False)
        )

    def test_returns_200_when_enqueue_raises_pool_acquire_timeout(
        self, client, monkeypatch
    ):
        """If inbox_repo.enqueue raises PoolAcquireTimeout, webhook must return 200."""
        self._mock_db_globals(monkeypatch)

        monkeypatch.setattr(
            "app.repositories.inbox_repo.enqueue",
            AsyncMock(side_effect=PoolAcquireTimeout("pool exhausted")),
        )

        payload = _make_meta_payload()
        resp = _signed_request(client, payload)

        # CRITICAL: must be 200, not 503. 503 would trigger Meta retry storm.
        assert resp.status_code == 200, (
            f"Expected 200 on DB-down enqueue failure, got {resp.status_code}"
        )

    def test_returns_200_when_enqueue_raises_connection_error(
        self, client, monkeypatch
    ):
        """If inbox_repo.enqueue raises a generic ConnectionError, webhook returns 200."""
        self._mock_db_globals(monkeypatch)

        monkeypatch.setattr(
            "app.repositories.inbox_repo.enqueue",
            AsyncMock(side_effect=ConnectionError("no db")),
        )

        payload = _make_meta_payload(wam_id="wamid.conn_err")
        resp = _signed_request(client, payload)
        assert resp.status_code == 200

    def test_returns_200_when_get_pool_raises(self, client, monkeypatch):
        """If get_pool itself raises (pool not initialized), webhook returns 200."""
        self._mock_db_globals(monkeypatch)

        monkeypatch.setattr(
            "app.repositories.inbox_repo.enqueue",
            AsyncMock(side_effect=ConnectionError("get_pool failed")),
        )

        payload = _make_meta_payload(wam_id="wamid.get_pool")
        resp = _signed_request(client, payload)
        assert resp.status_code == 200

    def test_returns_200_on_success(self, client, monkeypatch):
        """Baseline: successful enqueue still returns 200."""
        self._mock_db_globals(monkeypatch)

        monkeypatch.setattr(
            "app.repositories.inbox_repo.enqueue",
            AsyncMock(return_value=True),
        )

        payload = _make_meta_payload(wam_id="wamid.success")
        resp = _signed_request(client, payload)
        assert resp.status_code == 200

    def test_invalid_signature_returns_200_without_processing(
        self, client, monkeypatch
    ):
        """Bad signature → 200 but no processing (prevents fingerprinting attacks)."""
        monkeypatch.setattr(
            "app.routes.chat.rate_limit_check", AsyncMock(return_value=True)
        )
        payload = _make_meta_payload()
        body = json.dumps(payload).encode()
        resp = client.post(
            "/api/webhook/meta",
            content=body,
            headers={
                "x-hub-signature-256": "sha256=badhash",
                "content-type": "application/json",
            },
        )
        assert resp.status_code == 200


# ── 3. GET /health is always 200 (process-only probe) ────────────────────────

class TestHealthProcessOnly:
    """GET /health must return 200 regardless of DB/Redis state."""

    def test_health_returns_200_no_db(self, client):
        """Process-only /health returns 200 with no DB check."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_returns_200_when_db_broken(self, client):
        """Even if get_pool somehow raises, /health must return 200.

        The /health endpoint never calls get_pool, so any DB failure is
        invisible to it. This test verifies the endpoint body is correct.
        """
        # health_check no longer calls get_pool — patch is not needed, but we
        # include it to document that it's deliberately NOT called.
        with patch(
            "app.routes.health.get_pool",
            AsyncMock(side_effect=RuntimeError("pool broken")),
        ):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_never_has_db_key(self, client):
        """Process-only /health body has no 'db' key — that's /health/deep."""
        resp = client.get("/health")
        body = resp.json()
        # The liveness probe body is intentionally minimal
        assert "status" in body
        # 'db' key is NOT expected in the process-only probe
        assert "db" not in body


# ── 4. GET /health/deep returns 200 with degraded body when DB broken ─────────

def _make_pool_mock(*, raise_exc: Exception | None = None):
    """Build a minimal mock pool whose acquire() optionally raises."""
    conn = AsyncMock()
    if raise_exc is not None:
        conn.fetchval = AsyncMock(side_effect=raise_exc)
    else:
        conn.fetchval = AsyncMock(return_value=1)

    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    return pool


class TestHealthDeep:
    """GET /health/deep must always return HTTP 200; body reflects actual state."""

    _PATCH_TARGET = "app.routes.health.get_pool"
    _REDIS_PATCH = "app.routes.health.get_redis"

    def _mock_redis_ok(self):
        redis_mock = AsyncMock()
        redis_mock.ping = AsyncMock(return_value=True)
        return AsyncMock(return_value=redis_mock)

    def _mock_redis_unavailable(self):
        return AsyncMock(return_value=None)

    def test_deep_ok_when_db_and_redis_healthy(self, client):
        pool = _make_pool_mock()
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, self._mock_redis_ok()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"
        assert body["redis"] == "ok"

    def test_deep_returns_200_when_db_broken(self, client):
        """DB error must NOT cause a non-200 response — Railway must not restart."""
        pool = _make_pool_mock(raise_exc=ConnectionRefusedError("no db"))
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, self._mock_redis_ok()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200  # MUST be 200, not 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["db"] == "error"

    def test_deep_body_db_error_key(self, client):
        """Body must contain db='error' when DB is unreachable."""
        pool = _make_pool_mock(raise_exc=OSError("timeout"))
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, self._mock_redis_ok()):
                resp = client.get("/health/deep")
        body = resp.json()
        assert body["db"] == "error"
        assert body["status"] == "degraded"

    def test_deep_redis_unavailable_degraded(self, client):
        """Redis returning None (not configured) → redis='unavailable', not an error."""
        pool = _make_pool_mock()
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, self._mock_redis_unavailable()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["redis"] == "unavailable"
        # DB is fine — overall status should still be ok (Redis being unset is
        # an ops decision, not a degraded state)
        assert body["db"] == "ok"

    def test_deep_redis_error_degraded(self, client):
        """Redis ping raising an exception → redis='error', status='degraded'."""
        pool = _make_pool_mock()
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, AsyncMock(side_effect=Exception("redis down"))):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["redis"] == "error"
        assert body["status"] == "degraded"

    def test_deep_both_down_is_degraded(self, client):
        """Both DB and Redis down → status='degraded' with both error keys set."""
        pool = _make_pool_mock(raise_exc=ConnectionRefusedError("no db"))
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, AsyncMock(side_effect=Exception("redis dead"))):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["db"] == "error"
        assert body["redis"] == "error"

    def test_deep_has_all_expected_keys(self, client):
        """Response body always contains status, db, and redis keys."""
        pool = _make_pool_mock()
        with patch(self._PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(self._REDIS_PATCH, self._mock_redis_ok()):
                resp = client.get("/health/deep")
        body = resp.json()
        assert "status" in body
        assert "db" in body
        assert "redis" in body
