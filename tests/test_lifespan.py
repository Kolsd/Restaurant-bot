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
             patch("app.services.scheduler.start_scheduler", new_callable=AsyncMock), \
             patch("app.services.redis_client.close_redis", new_callable=AsyncMock):
            mock_db.init_pool = AsyncMock()
            mock_db.db_cleanup_expired_sessions = AsyncMock()

            from fastapi.testclient import TestClient
            from app.main import app
            with TestClient(app):
                pass  # lifespan ran without error
