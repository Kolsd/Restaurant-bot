"""
tests/test_scheduler_leader.py

Tests for scheduler leader election via state_store.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services import state_store


class TestSchedulerLeaderAcquire:

    async def test_first_acquire_succeeds(self):
        """First call should return True (leader acquired)."""
        # Fallback path (no Redis) always returns True
        result = await state_store.scheduler_leader_acquire(ttl_seconds=5)
        assert result is True

    async def test_redis_nx_returns_true_on_first_call(self):
        """With Redis mock, SET NX returns True on first call."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert result is True
        mock_redis.set.assert_called_once_with("mesio:scheduler_leader", "1", nx=True, ex=90)

    async def test_redis_nx_returns_false_when_held(self):
        """With Redis mock, SET NX returns None when lock is already held."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert result is False

    async def test_fallback_returns_true(self):
        """Without Redis, fallback always returns True (run everywhere)."""
        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=None)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert result is True
