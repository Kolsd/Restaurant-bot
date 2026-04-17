"""
tests/test_scheduler_leader.py

Tests for scheduler leader election via state_store.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services import state_store


class TestSchedulerLeaderAcquire:

    async def test_first_acquire_succeeds(self):
        """Fallback path (no Redis) returns truthy sentinel token."""
        result = await state_store.scheduler_leader_acquire(ttl_seconds=5)
        assert result  # truthy — "fallback" sentinel

    async def test_redis_nx_returns_token_on_first_call(self):
        """With Redis mock, SET NX success returns a UUID token string."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=True)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert isinstance(result, str)
        assert len(result) == 36  # UUID4 format
        call_args = mock_redis.set.call_args
        assert call_args[0][0] == "mesio:scheduler_leader"
        assert call_args[1]["nx"] is True
        assert call_args[1]["ex"] == 90
        # The stored value is the token itself, not "1"
        stored_token = call_args[0][1]
        assert stored_token == result

    async def test_redis_nx_returns_none_when_held(self):
        """With Redis mock, SET NX failure returns None (falsy — not leader)."""
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock(return_value=None)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert result is None

    async def test_fallback_returns_sentinel_token(self):
        """Without Redis, returns the 'fallback' sentinel (truthy)."""
        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=None)):
            result = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert result == "fallback"
