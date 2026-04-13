"""
tests/test_rate_limit.py

Tests for the rate_limit_check function in state_store.
"""
import time
import pytest
from unittest.mock import AsyncMock, patch

from app.services import state_store


class TestRateLimitCheck:
    """Unit tests for rate_limit_check — Redis mocked as unavailable (fallback path)."""

    async def test_allows_under_limit(self):
        """Requests under the limit should be allowed."""
        key = f"test_allow_{time.monotonic()}"
        assert await state_store.rate_limit_check(key, max_requests=3, window_seconds=10) is True

    async def test_blocks_over_limit(self):
        """Requests exceeding the limit should be blocked."""
        key = f"test_block_{time.monotonic()}"
        for _ in range(3):
            await state_store.rate_limit_check(key, max_requests=3, window_seconds=10)
        # 4th request should be blocked
        assert await state_store.rate_limit_check(key, max_requests=3, window_seconds=10) is False

    async def test_different_keys_independent(self):
        """Rate limits for different keys don't interfere."""
        key_a = f"test_a_{time.monotonic()}"
        key_b = f"test_b_{time.monotonic()}"
        for _ in range(3):
            await state_store.rate_limit_check(key_a, max_requests=3, window_seconds=10)
        # key_a is at limit, key_b should still be allowed
        assert await state_store.rate_limit_check(key_a, max_requests=3, window_seconds=10) is False
        assert await state_store.rate_limit_check(key_b, max_requests=3, window_seconds=10) is True


class TestPayCheckRateLimit:
    """Test that pay_check endpoint returns 429 when rate limited."""

    def test_pay_check_429_on_rate_limit(self, client, monkeypatch):
        """Rapid pay requests should return 429."""
        from app.services import state_store as ss

        # Mock rate_limit_check to always return False (blocked)
        monkeypatch.setattr(ss, "rate_limit_check", AsyncMock(return_value=False))

        # Mock auth
        monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="owner"))
        from app.services import database as db
        monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value={
            "username": "owner", "restaurant_name": "Test", "branch_id": 1, "role": "owner", "password_hash": "$2b$12$x"
        }))
        monkeypatch.setattr(db, "db_get_restaurant_by_id", AsyncMock(return_value={
            "id": 1, "name": "Test", "whatsapp_number": "+57300", "features": {}
        }))

        resp = client.post(
            "/api/table-orders/ORD123/checks/CHK456/pay",
            json={"payments": [{"method": "cash", "amount": 10000}]},
            headers={"Authorization": "Bearer fake-token"}
        )
        assert resp.status_code == 429
