"""
tests/test_health.py

Tests for GET /health.

Two modes:
  1. Unit (no DB env var) — patches get_pool on the health module to simulate
     healthy and degraded states, verifying response shape and status codes.
     Uses the shared `client` fixture (no lifespan, no real DB needed).
  2. Integration (DATABASE_URL set) — hits real PostgreSQL via TestClient
     with the full app startup. Skipped when no DATABASE_URL is available.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app

# The name to patch: get_pool is imported at module level in health.py,
# so we patch it in the health module's namespace.
_PATCH_TARGET = "app.routes.health.get_pool"


# ── Helper ─────────────────────────────────────────────────────────────────────

def _make_pool_mock(*, raise_exc: Exception | None = None):
    """Return a minimal mock pool whose acquire() optionally raises."""
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


# ── Unit tests (no real DB needed) ───────────────────────────────────────────
# Use the shared `client` fixture from conftest — it is TestClient(app)
# instantiated without a context manager, so lifespan events are NOT fired.

class TestHealthUnit:
    def test_healthy_returns_200(self, client):
        pool = _make_pool_mock()
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"

    def test_db_error_returns_503(self, client):
        pool = _make_pool_mock(raise_exc=ConnectionRefusedError("no db"))
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "ConnectionRefusedError" in body["db"]

    def test_get_pool_error_returns_503(self, client):
        """If get_pool itself raises (e.g. pool not initialised), still 503."""
        with patch(_PATCH_TARGET, AsyncMock(side_effect=RuntimeError("pool not init"))):
            resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "RuntimeError" in body["db"]

    def test_response_always_has_db_key(self, client):
        pool = _make_pool_mock()
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            resp = client.get("/health")
        assert "db" in resp.json()

    def test_method_not_allowed(self, client):
        """Only GET is registered — POST should return 405."""
        pool = _make_pool_mock()
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            resp = client.post("/health")
        assert resp.status_code == 405


# ── Integration test (real DB) ───────────────────────────────────────────────

class TestHealthIntegration:
    """
    Hits a real PostgreSQL server.  Skipped when no DATABASE_URL is available.

    Uses TestClient as a context manager to trigger the full lifespan so that
    the DB pool is initialised before the request is made.
    """

    @pytest.fixture(autouse=True)
    def require_db(self):
        url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not url:
            pytest.skip("No DATABASE_URL set — skipping integration test")
        # Reset the pool singleton so TestClient's lifespan can re-init cleanly
        import app.services.database as _db
        _db._pool = None
        # Ensure db.init_pool() (called during app startup) picks up the right URL.
        test_url = os.environ.get("TEST_DATABASE_URL")
        original = os.environ.get("DATABASE_URL")
        if test_url and test_url != original:
            os.environ["DATABASE_URL"] = test_url
            yield
            if original is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = original
        else:
            yield
        _db._pool = None

    def test_health_response_shape(self):
        """Response must always have 'status' and 'db' keys."""
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert "status" in body
        assert "db" in body

    def test_health_db_ok_when_reachable(self):
        """When DATABASE_URL points to a live server, db should be 'ok'."""
        with TestClient(app) as client:
            resp = client.get("/health")
        if resp.status_code == 200:
            assert resp.json()["db"] == "ok"


# ── Metrics tests (mocked, no real DB) ───────────────────────────────────────

class TestHealthMetrics:
    """Tests for GET /health/metrics."""

    def test_metrics_requires_auth(self, client):
        """No auth header → 401."""
        resp = client.get("/health/metrics")
        assert resp.status_code == 401

    def test_metrics_wrong_key(self, client, monkeypatch):
        """Wrong ADMIN_KEY → 401."""
        monkeypatch.setenv("ADMIN_KEY", "correct-key")
        resp = client.get("/health/metrics", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 401

    def test_metrics_returns_data(self, client, monkeypatch):
        """Valid auth → 200 with expected keys (infrastructure metrics)."""
        monkeypatch.setenv("ADMIN_KEY", "test-key-123")

        # The metrics endpoint calls get_pool() eight times:
        #   1. pool stats (get_size / get_idle_size, no acquire)
        #   2. inbox_queue_depth
        #   3. inbox_dead_letters
        #   4. orders_today
        #   5. active_table_sessions
        #   6. active_conversations
        #   7. restaurants_total
        #   8. staff_clocked_in
        def _make_conn_mock(return_value):
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=return_value)
            acquire_cm = AsyncMock()
            acquire_cm.__aenter__ = AsyncMock(return_value=conn)
            acquire_cm.__aexit__ = AsyncMock(return_value=False)
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=acquire_cm)
            return pool

        stats_pool = AsyncMock()
        stats_pool.get_size = MagicMock(return_value=20)
        stats_pool.get_idle_size = MagicMock(return_value=18)

        get_pool_mock = AsyncMock(side_effect=[
            stats_pool,
            _make_conn_mock(5),   # inbox_queue_depth
            _make_conn_mock(2),   # inbox_dead_letters
            _make_conn_mock(42),  # orders_today
            _make_conn_mock(3),   # active_table_sessions
            _make_conn_mock(7),   # active_conversations
            _make_conn_mock(10),  # restaurants_total
            _make_conn_mock(4),   # staff_clocked_in
        ])

        with patch(_PATCH_TARGET, get_pool_mock):
            resp = client.get("/health/metrics", headers={"Authorization": "Bearer test-key-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert "db_pool_size" in body
        assert "db_pool_free" in body
        assert "db_pool_used" in body
        assert "inbox_queue_depth" in body
        assert "inbox_dead_letters" in body

    def test_metrics_pool_values_correct(self, client, monkeypatch):
        """Pool size arithmetic is correct: used = size - free."""
        monkeypatch.setenv("ADMIN_KEY", "test-key-123")

        def _make_conn_mock(return_value):
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=return_value)
            acquire_cm = AsyncMock()
            acquire_cm.__aenter__ = AsyncMock(return_value=conn)
            acquire_cm.__aexit__ = AsyncMock(return_value=False)
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=acquire_cm)
            return pool

        stats_pool = AsyncMock()
        stats_pool.get_size = MagicMock(return_value=10)
        stats_pool.get_idle_size = MagicMock(return_value=7)

        get_pool_mock = AsyncMock(side_effect=[
            stats_pool,
            _make_conn_mock(0),  # inbox_queue_depth
            _make_conn_mock(0),  # inbox_dead_letters
            _make_conn_mock(0),  # orders_today
            _make_conn_mock(0),  # active_table_sessions
            _make_conn_mock(0),  # active_conversations
            _make_conn_mock(0),  # restaurants_total
            _make_conn_mock(0),  # staff_clocked_in
        ])

        with patch(_PATCH_TARGET, get_pool_mock):
            resp = client.get("/health/metrics", headers={"Authorization": "Bearer test-key-123"})

        body = resp.json()
        assert body["db_pool_size"] == 10
        assert body["db_pool_free"] == 7
        assert body["db_pool_used"] == 3

    def test_metrics_no_admin_key_env(self, client, monkeypatch):
        """If ADMIN_KEY env is not set at all, any request returns 401."""
        monkeypatch.delenv("ADMIN_KEY", raising=False)
        resp = client.get("/health/metrics", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 401

    def test_metrics_business_counters(self, client, monkeypatch):
        """Business metrics appear in response with correct values."""
        monkeypatch.setenv("ADMIN_KEY", "test-key-123")

        def _make_conn_mock(return_value):
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=return_value)
            acquire_cm = AsyncMock()
            acquire_cm.__aenter__ = AsyncMock(return_value=conn)
            acquire_cm.__aexit__ = AsyncMock(return_value=False)
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=acquire_cm)
            return pool

        stats_pool = AsyncMock()
        stats_pool.get_size = MagicMock(return_value=5)
        stats_pool.get_idle_size = MagicMock(return_value=5)

        get_pool_mock = AsyncMock(side_effect=[
            stats_pool,
            _make_conn_mock(0),    # inbox_queue_depth
            _make_conn_mock(0),    # inbox_dead_letters
            _make_conn_mock(17),   # orders_today
            _make_conn_mock(6),    # active_table_sessions
            _make_conn_mock(11),   # active_conversations
            _make_conn_mock(3),    # restaurants_total
            _make_conn_mock(8),    # staff_clocked_in
        ])

        with patch(_PATCH_TARGET, get_pool_mock):
            resp = client.get("/health/metrics", headers={"Authorization": "Bearer test-key-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["orders_today"] == 17
        assert body["active_table_sessions"] == 6
        assert body["active_conversations"] == 11
        assert body["restaurants_total"] == 3
        assert body["staff_clocked_in"] == 8

    def test_metrics_business_counters_fallback_to_none_on_error(self, client, monkeypatch):
        """If a business metric query fails, its value is None and other metrics still present."""
        monkeypatch.setenv("ADMIN_KEY", "test-key-123")

        def _make_conn_mock(return_value):
            conn = AsyncMock()
            conn.fetchval = AsyncMock(return_value=return_value)
            acquire_cm = AsyncMock()
            acquire_cm.__aenter__ = AsyncMock(return_value=conn)
            acquire_cm.__aexit__ = AsyncMock(return_value=False)
            pool = AsyncMock()
            pool.acquire = MagicMock(return_value=acquire_cm)
            return pool

        def _make_error_pool():
            pool = AsyncMock()
            pool.acquire = MagicMock(side_effect=Exception("DB unavailable"))
            return pool

        stats_pool = AsyncMock()
        stats_pool.get_size = MagicMock(return_value=5)
        stats_pool.get_idle_size = MagicMock(return_value=5)

        get_pool_mock = AsyncMock(side_effect=[
            stats_pool,
            _make_conn_mock(1),    # inbox_queue_depth — ok
            _make_conn_mock(0),    # inbox_dead_letters — ok
            _make_error_pool(),    # orders_today — fails
            _make_conn_mock(2),    # active_table_sessions — ok
            _make_conn_mock(3),    # active_conversations — ok
            _make_conn_mock(5),    # restaurants_total — ok
            _make_conn_mock(1),    # staff_clocked_in — ok
        ])

        with patch(_PATCH_TARGET, get_pool_mock):
            resp = client.get("/health/metrics", headers={"Authorization": "Bearer test-key-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["orders_today"] is None          # failed → None
        assert body["active_table_sessions"] == 2    # subsequent metrics still work
        assert body["active_conversations"] == 3
        assert body["restaurants_total"] == 5
        assert body["staff_clocked_in"] == 1
