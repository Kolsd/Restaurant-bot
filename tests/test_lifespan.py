"""
tests/test_lifespan.py

Smoke tests for the modernised lifespan (Phase 2.1).
Verifies that startup/shutdown logic runs without errors.
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestLifespan:
    """Unit tests for lifespan — all external deps mocked."""

    def test_app_starts_without_error(self):
        """TestClient as context manager triggers lifespan."""
        with patch("app.main.db") as mock_db, \
             patch("app.services.inbox_worker.run_worker", new_callable=AsyncMock) as mock_worker, \
             patch("app.services.scheduler.start_scheduler", new_callable=AsyncMock), \
             patch("app.services.redis_client.close_redis", new_callable=AsyncMock):
            mock_db.init_pool = AsyncMock()
            mock_db.db_cleanup_expired_sessions = AsyncMock()
            mock_worker.return_value = None

            from fastapi.testclient import TestClient
            from app.main import app
            with TestClient(app):
                pass  # lifespan ran without error

    def test_worker_disabled_by_env(self, monkeypatch):
        """When DISABLE_EMBEDDED_WORKER=1, inbox worker should not start."""
        monkeypatch.setenv("DISABLE_EMBEDDED_WORKER", "1")

        with patch("app.main.db") as mock_db, \
             patch("app.services.inbox_worker.run_worker", new_callable=AsyncMock) as mock_worker, \
             patch("app.services.scheduler.start_scheduler", new_callable=AsyncMock), \
             patch("app.services.redis_client.close_redis", new_callable=AsyncMock):
            mock_db.init_pool = AsyncMock()
            mock_db.db_cleanup_expired_sessions = AsyncMock()

            from fastapi.testclient import TestClient
            from app.main import app
            with TestClient(app):
                pass

            mock_worker.assert_not_called()

    def test_worker_enabled_by_default(self, monkeypatch):
        """When DISABLE_EMBEDDED_WORKER is not set, inbox worker task is created."""
        monkeypatch.delenv("DISABLE_EMBEDDED_WORKER", raising=False)

        # run_worker must return a coroutine that completes so the task doesn't hang.
        async def _noop(_stop_event):
            return

        with patch("app.main.db") as mock_db, \
             patch("app.services.inbox_worker.run_worker", side_effect=_noop) as mock_worker, \
             patch("app.services.scheduler.start_scheduler", new_callable=AsyncMock), \
             patch("app.services.redis_client.close_redis", new_callable=AsyncMock):
            mock_db.init_pool = AsyncMock()
            mock_db.db_cleanup_expired_sessions = AsyncMock()

            from fastapi.testclient import TestClient
            from app.main import app
            with TestClient(app):
                pass

            mock_worker.assert_called_once()
