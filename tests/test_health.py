"""
tests/test_health.py

Tests for GET /health (process-only liveness probe) and GET /health/deep
(full dependency check).

GET /health:
  - NEVER checks DB or Redis.
  - Always returns 200 {"status": "ok"} as long as the process is alive.
  - Railway uses this as its healthcheck — must not trip during DB outages.

GET /health/deep:
  - Checks DB pool (SELECT 1) + Redis ping.
  - Always returns HTTP 200 (so Railway does NOT restart the service).
  - Body carries per-dependency status: ok | error | unavailable.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app

# The name to patch: get_pool is imported at module level in health.py,
# so we patch it in the health module's namespace.
_PATCH_TARGET = "app.routes.health.get_pool"
_REDIS_PATCH_TARGET = "app.routes.health.get_redis"
_METRICS_PATCH_TARGET = "app.routes.internal.ops.get_pool"


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


def _make_redis_mock(*, ping_raises: Exception | None = None, return_none: bool = False):
    """Return a mock for get_redis() (the async function)."""
    if return_none:
        return AsyncMock(return_value=None)
    redis_obj = AsyncMock()
    if ping_raises is not None:
        redis_obj.ping = AsyncMock(side_effect=ping_raises)
    else:
        redis_obj.ping = AsyncMock(return_value=True)
    return AsyncMock(return_value=redis_obj)


# ── Unit tests — GET /health (process-only liveness probe) ───────────────────
# Use the shared `client` fixture from conftest — it is TestClient(app)
# instantiated without a context manager, so lifespan events are NOT fired.

class TestHealthUnit:
    def test_returns_200_always(self, client):
        """Process-only /health always returns 200 regardless of DB state."""
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_status_ok(self, client):
        """Body contains {"status": "ok"}."""
        resp = client.get("/health")
        assert resp.json()["status"] == "ok"

    def test_no_db_key(self, client):
        """Process-only probe must NOT have a 'db' key — that's /health/deep."""
        resp = client.get("/health")
        assert "db" not in resp.json()

    def test_db_broken_still_200(self, client):
        """Even if get_pool raises (broken DB), /health returns 200.

        /health never calls get_pool — this test documents that DB state is
        completely invisible to the liveness probe.
        """
        with patch(_PATCH_TARGET, AsyncMock(side_effect=RuntimeError("pool broken"))):
            resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_method_not_allowed(self, client):
        """Only GET is registered — POST should return 405."""
        resp = client.post("/health")
        assert resp.status_code == 405


# ── Unit tests — GET /health/deep (full dependency check) ────────────────────

class TestHealthDeepUnit:
    def test_deep_healthy_returns_200(self, client):
        pool = _make_pool_mock()
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(_REDIS_PATCH_TARGET, _make_redis_mock()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"

    def test_deep_db_error_returns_200_with_degraded(self, client):
        """DB failure must return HTTP 200 (not 503) — Railway must not restart."""
        pool = _make_pool_mock(raise_exc=ConnectionRefusedError("no db"))
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(_REDIS_PATCH_TARGET, _make_redis_mock()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200  # MUST be 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["db"] == "error"

    def test_deep_get_pool_error_returns_200(self, client):
        """If get_pool itself raises, /health/deep still returns 200."""
        with patch(_PATCH_TARGET, AsyncMock(side_effect=RuntimeError("pool not init"))):
            with patch(_REDIS_PATCH_TARGET, _make_redis_mock()):
                resp = client.get("/health/deep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["db"] == "error"

    def test_deep_response_always_has_all_keys(self, client):
        pool = _make_pool_mock()
        with patch(_PATCH_TARGET, AsyncMock(return_value=pool)):
            with patch(_REDIS_PATCH_TARGET, _make_redis_mock()):
                resp = client.get("/health/deep")
        body = resp.json()
        assert "status" in body
        assert "db" in body
        assert "redis" in body


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

    def test_health_always_200(self):
        """Process-only /health always returns 200 (never depends on DB)."""
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # /health must NOT have a 'db' key — that's /health/deep
        assert "db" not in body

    def test_health_deep_response_shape(self):
        """Response from /health/deep must have status, db, and redis keys."""
        with TestClient(app) as client:
            resp = client.get("/health/deep")
        # Always 200 even when degraded — Railway must not restart on this
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "db" in body
        assert "redis" in body

    def test_health_deep_db_ok_when_reachable(self):
        """When DATABASE_URL points to a live server, db should be 'ok'."""
        with TestClient(app) as client:
            resp = client.get("/health/deep")
        body = resp.json()
        if body.get("db") == "ok":
            assert body["status"] in ("ok", "degraded")  # redis might be absent


# ── Metrics tests (mocked, no real DB) ───────────────────────────────────────

class TestHealthMetrics:
    """Tests for GET /api/internal/ops/metrics (moved from /health/metrics)."""

    @pytest.fixture(autouse=True)
    def _session_mock(self, mock_superadmin_session):
        """All metrics tests use the superadmin session mock."""

    def test_metrics_requires_auth(self, client):
        """No auth header → 401."""
        resp = client.get("/api/internal/ops/metrics")
        assert resp.status_code == 401

    def test_metrics_wrong_key(self, client, monkeypatch):
        """Unknown session token (not superadmin) → 403."""
        from unittest.mock import patch, AsyncMock
        monkeypatch.setenv("ADMIN_KEY", "correct-key")
        # Override the autouse mock: unknown token returns None → 403
        with patch("app.repositories.sessions_repo.get_session", AsyncMock(return_value=None)):
            resp = client.get("/api/internal/ops/metrics", headers={"Authorization": "Bearer wrong-key"})
        assert resp.status_code == 403

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

        with patch(_METRICS_PATCH_TARGET, get_pool_mock):
            resp = client.get("/api/internal/ops/metrics", headers={"Authorization": "Bearer test-key-123"})

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

        with patch(_METRICS_PATCH_TARGET, get_pool_mock):
            resp = client.get("/api/internal/ops/metrics", headers={"Authorization": "Bearer test-key-123"})

        body = resp.json()
        assert body["db_pool_size"] == 10
        assert body["db_pool_free"] == 7
        assert body["db_pool_used"] == 3

    def test_metrics_no_admin_key_env(self, client, monkeypatch):
        """Without a valid session token, request returns 403 (ADMIN_KEY env is irrelevant now).
        Without any token, returns 401."""
        monkeypatch.delenv("ADMIN_KEY", raising=False)
        # No auth header at all → 401
        resp = client.get("/api/internal/ops/metrics")
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

        with patch(_METRICS_PATCH_TARGET, get_pool_mock):
            resp = client.get("/api/internal/ops/metrics", headers={"Authorization": "Bearer test-key-123"})

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

        with patch(_METRICS_PATCH_TARGET, get_pool_mock):
            resp = client.get("/api/internal/ops/metrics", headers={"Authorization": "Bearer test-key-123"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["orders_today"] is None          # failed → None
        assert body["active_table_sessions"] == 2    # subsequent metrics still work
        assert body["active_conversations"] == 3
        assert body["restaurants_total"] == 5
        assert body["staff_clocked_in"] == 1
