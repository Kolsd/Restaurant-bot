"""
tests/test_scheduler_leader_renewal.py

Tests for scheduler leader lease renewal and mid-tick abort behaviour.

Covers:
  1. scheduler_leader_renew — Redis token match extends TTL (returns True).
  2. scheduler_leader_renew — Redis token mismatch returns False.
  3. scheduler_leader_renew — Redis down returns True (fallback).
  4. scheduler_leader_renew — "fallback" sentinel always returns True.
  5. _scheduler_loop breaks out when renew returns False after a phase.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.services import state_store
from app.services.scheduler import _scheduler_loop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis(get_value: str | None, eval_result: int = 1) -> AsyncMock:
    """Return an AsyncMock Redis client for renew tests."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=get_value)
    mock.eval = AsyncMock(return_value=eval_result)
    return mock


# ---------------------------------------------------------------------------
# scheduler_leader_renew unit tests
# ---------------------------------------------------------------------------


class TestSchedulerLeaderRenew:

    async def test_renew_succeeds_when_token_matches(self):
        """Lua script returns 1 → True (lease renewed)."""
        token = "abc-123"
        mock_redis = _make_redis(get_value=token, eval_result=1)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_renew(token, ttl_seconds=90)

        assert result is True
        mock_redis.eval.assert_awaited_once()
        # Verify the Lua was called with the correct key and token
        args = mock_redis.eval.call_args[0]
        assert args[1] == 1  # numkeys=1
        assert args[2] == state_store._SCHEDULER_LEADER_KEY
        assert args[3] == token
        assert args[4] == "90000"  # TTL in milliseconds

    async def test_renew_fails_when_token_mismatches(self):
        """Lua script returns 0 → False (lease stolen by another worker)."""
        token = "abc-123"
        mock_redis = _make_redis(get_value="other-worker-token", eval_result=0)

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_renew(token, ttl_seconds=90)

        assert result is False

    async def test_renew_fallback_token_always_true(self):
        """'fallback' sentinel skips Redis entirely and returns True."""
        mock_redis = AsyncMock()
        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_renew("fallback", ttl_seconds=90)

        assert result is True
        mock_redis.eval.assert_not_awaited()

    async def test_renew_redis_down_returns_true(self):
        """If Redis is unavailable, renew returns True (conservative — keep running)."""
        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=None)):
            result = await state_store.scheduler_leader_renew("some-real-token", ttl_seconds=90)

        assert result is True

    async def test_renew_redis_exception_returns_true(self):
        """If r.eval raises, renew returns True to avoid orphaning a partial tick."""
        token = "abc-123"
        mock_redis = AsyncMock()
        mock_redis.eval = AsyncMock(side_effect=RuntimeError("redis timeout"))

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            result = await state_store.scheduler_leader_renew(token, ttl_seconds=90)

        assert result is True


# ---------------------------------------------------------------------------
# _scheduler_loop integration tests (loop break-out on renew failure)
# ---------------------------------------------------------------------------


class TestSchedulerLoopLeaderRenewal:
    """
    Drive _scheduler_loop for a single tick and assert break-out behaviour
    when scheduler_leader_renew returns False at various phase boundaries.
    """

    def _make_loop_patches(
        self,
        *,
        renew_returns: list[bool],
        leader_token: str = "test-token-abc",
    ) -> dict:
        """
        Build the minimal set of patches to run one loop iteration without real I/O.

        renew_returns: sequence of bool values returned by successive _renew_or_abort calls.
        """
        renew_iter = iter(renew_returns)

        async def fake_sleep(seconds):
            # Let the loop sleep exactly once, then cancel on second sleep.
            raise asyncio.CancelledError()

        patches = {
            "asyncio.sleep": patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            "acquire": patch(
                "app.services.state_store.scheduler_leader_acquire",
                new=AsyncMock(return_value=leader_token),
            ),
            "renew": patch(
                "app.services.scheduler._renew_or_abort",
                new=AsyncMock(side_effect=lambda token, ttl_seconds=90: (
                    asyncio.coroutine(lambda: next(renew_iter, True))()
                )),
            ),
            "inactivity": patch(
                "app.services.scheduler._run_inactivity_check",
                new=AsyncMock(),
            ),
            "alerts": patch(
                "app.services.scheduler.check_alerts",
                new=AsyncMock(),
            ),
            "reminders": patch(
                "app.services.scheduler._run_reservation_reminders",
                new=AsyncMock(),
            ),
            "deposit": patch(
                "app.services.scheduler._run_deposit_expiry",
                new=AsyncMock(),
            ),
            "occupancy": patch(
                "app.services.scheduler._run_occupancy_snapshot",
                new=AsyncMock(),
            ),
            "weekly": patch(
                "app.services.scheduler._run_weekly_owner_reports",
                new=AsyncMock(),
            ),
            "bypass": patch(
                "app.services.scheduler._scheduler_loop.__globals__['bypass_tenant_scope']",
                # bypass_tenant_scope is imported inside _scheduler_loop, so mock via module
                create=True,
            ),
        }
        return patches

    async def _run_one_tick(self, renew_returns: list[bool]) -> dict[str, AsyncMock]:
        """
        Execute one tick of _scheduler_loop (with CancelledError stopping the loop)
        and return the mock objects keyed by name for assertion.
        """
        renew_call_count = 0
        renew_values = list(renew_returns)

        async def fake_renew(token, ttl_seconds=90):
            nonlocal renew_call_count
            idx = renew_call_count
            renew_call_count += 1
            if idx < len(renew_values):
                return renew_values[idx]
            return True

        # We need to also cancel the while loop after the first iteration.
        # fake_sleep raises CancelledError on first call → loop exits.
        sleep_call_count = 0

        async def fake_sleep(seconds):
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                raise asyncio.CancelledError()
            # First sleep: proceed normally (return immediately for speed)

        mock_inactivity = AsyncMock()
        mock_alerts = AsyncMock()
        mock_reminders = AsyncMock()
        mock_deposit = AsyncMock()
        mock_occupancy = AsyncMock()
        mock_weekly = AsyncMock()
        mock_acquire = AsyncMock(return_value="test-token-abc")
        mock_renew = AsyncMock(side_effect=fake_renew)
        # bypass_tenant_scope must be a context manager
        bypass_cm = MagicMock()
        bypass_cm.__enter__ = MagicMock(return_value=None)
        bypass_cm.__exit__ = MagicMock(return_value=False)
        mock_bypass = MagicMock(return_value=bypass_cm)

        with (
            patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("app.services.state_store.scheduler_leader_acquire", mock_acquire),
            patch("app.services.scheduler._renew_or_abort", mock_renew),
            patch("app.services.scheduler._run_inactivity_check", mock_inactivity),
            patch("app.services.scheduler._run_weekly_owner_reports", mock_weekly),
        ):
            # Import check_alerts lazily inside the loop; patch at module level
            import app.services.alerts as alerts_mod
            with patch.object(alerts_mod, "check_alerts", mock_alerts):
                # Also patch the reservation/deposit/occupancy funcs
                with (
                    patch("app.services.scheduler._run_reservation_reminders", mock_reminders),
                    patch("app.services.scheduler._run_deposit_expiry", mock_deposit),
                    patch("app.services.scheduler._run_occupancy_snapshot", mock_occupancy),
                ):
                    # Patch bypass_tenant_scope inside the scheduler module namespace
                    import app.services.scheduler as sched_mod
                    from app.services.tenant_context import bypass_tenant_scope as real_bypass
                    with patch.object(sched_mod, "_renew_or_abort", mock_renew):
                        try:
                            await asyncio.wait_for(_scheduler_loop(), timeout=5.0)
                        except (asyncio.CancelledError, asyncio.TimeoutError):
                            pass

        return {
            "inactivity": mock_inactivity,
            "alerts": mock_alerts,
            "reminders": mock_reminders,
            "deposit": mock_deposit,
            "occupancy": mock_occupancy,
            "weekly": mock_weekly,
            "renew": mock_renew,
            "renew_call_count": renew_call_count,
        }

    async def test_normal_tick_completes_all_phases(self):
        """When all renews return True, the full tick runs."""
        # Patch everything cleanly for one full tick
        sleep_call = 0

        async def fake_sleep(s):
            nonlocal sleep_call
            sleep_call += 1
            if sleep_call >= 2:
                raise asyncio.CancelledError()

        mock_inactivity = AsyncMock()
        mock_alerts = AsyncMock()
        mock_weekly = AsyncMock()
        mock_acquire = AsyncMock(return_value="tok-abc")
        # All renews return True
        mock_renew = AsyncMock(return_value=True)

        bypass_cm = MagicMock()
        bypass_cm.__enter__ = MagicMock(return_value=None)
        bypass_cm.__exit__ = MagicMock(return_value=False)

        import app.services.alerts as alerts_mod
        import app.services.scheduler as sched_mod

        with (
            patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("app.services.state_store.scheduler_leader_acquire", mock_acquire),
            patch("app.services.scheduler._renew_or_abort", mock_renew),
            patch("app.services.scheduler._run_inactivity_check", mock_inactivity),
            patch("app.services.scheduler._run_weekly_owner_reports", mock_weekly),
            patch.object(alerts_mod, "check_alerts", mock_alerts),
        ):
            try:
                await asyncio.wait_for(_scheduler_loop(), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        mock_inactivity.assert_awaited_once()
        mock_alerts.assert_awaited_once()
        mock_weekly.assert_awaited_once()
        # At least 2 renew calls (after inactivity + after alerts)
        assert mock_renew.await_count >= 2

    async def test_tick_aborts_after_inactivity_if_renew_false(self):
        """If renew returns False after inactivity check, remaining phases are skipped."""
        sleep_call = 0

        async def fake_sleep(s):
            nonlocal sleep_call
            sleep_call += 1
            if sleep_call >= 2:
                raise asyncio.CancelledError()

        mock_inactivity = AsyncMock()
        mock_alerts = AsyncMock()
        mock_weekly = AsyncMock()
        mock_acquire = AsyncMock(return_value="tok-abc")
        # First renew (after inactivity) → False → abort
        renew_values = iter([False])
        mock_renew = AsyncMock(side_effect=lambda token, ttl_seconds=90: asyncio.coroutine(
            lambda: next(renew_values, True)
        )())

        # Re-implement with simpler side_effect
        renew_results = [False]
        renew_idx = 0

        async def renew_side_effect(token, ttl_seconds=90):
            nonlocal renew_idx
            val = renew_results[renew_idx] if renew_idx < len(renew_results) else True
            renew_idx += 1
            return val

        mock_renew = AsyncMock(side_effect=renew_side_effect)

        import app.services.alerts as alerts_mod

        with (
            patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("app.services.state_store.scheduler_leader_acquire", mock_acquire),
            patch("app.services.scheduler._renew_or_abort", mock_renew),
            patch("app.services.scheduler._run_inactivity_check", mock_inactivity),
            patch("app.services.scheduler._run_weekly_owner_reports", mock_weekly),
            patch.object(alerts_mod, "check_alerts", mock_alerts),
        ):
            try:
                await asyncio.wait_for(_scheduler_loop(), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        mock_inactivity.assert_awaited_once()
        # After renew returns False, alerts and weekly must NOT be called
        mock_alerts.assert_not_awaited()
        mock_weekly.assert_not_awaited()
        # Exactly one renew attempted
        assert mock_renew.await_count == 1

    async def test_tick_aborts_after_alerts_if_renew_false(self):
        """If renew returns False after alerts phase, weekly reports are skipped."""
        sleep_call = 0

        async def fake_sleep(s):
            nonlocal sleep_call
            sleep_call += 1
            if sleep_call >= 2:
                raise asyncio.CancelledError()

        mock_inactivity = AsyncMock()
        mock_alerts = AsyncMock()
        mock_weekly = AsyncMock()
        mock_acquire = AsyncMock(return_value="tok-abc")

        # First renew True (after inactivity), second renew False (after alerts)
        renew_results = [True, False]
        renew_idx = 0

        async def renew_side_effect(token, ttl_seconds=90):
            nonlocal renew_idx
            val = renew_results[renew_idx] if renew_idx < len(renew_results) else True
            renew_idx += 1
            return val

        mock_renew = AsyncMock(side_effect=renew_side_effect)

        import app.services.alerts as alerts_mod

        with (
            patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("app.services.state_store.scheduler_leader_acquire", mock_acquire),
            patch("app.services.scheduler._renew_or_abort", mock_renew),
            patch("app.services.scheduler._run_inactivity_check", mock_inactivity),
            patch("app.services.scheduler._run_weekly_owner_reports", mock_weekly),
            patch.object(alerts_mod, "check_alerts", mock_alerts),
        ):
            try:
                await asyncio.wait_for(_scheduler_loop(), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        mock_inactivity.assert_awaited_once()
        mock_alerts.assert_awaited_once()
        # Weekly must NOT run after second renew fails
        mock_weekly.assert_not_awaited()
        assert mock_renew.await_count == 2

    async def test_non_leader_worker_skips_tick(self):
        """When acquire returns None (not leader), the tick body is not entered."""
        sleep_call = 0

        async def fake_sleep(s):
            nonlocal sleep_call
            sleep_call += 1
            if sleep_call >= 2:
                raise asyncio.CancelledError()

        mock_inactivity = AsyncMock()
        # acquire returns None → not leader
        mock_acquire = AsyncMock(return_value=None)

        with (
            patch("app.services.scheduler.asyncio.sleep", side_effect=fake_sleep),
            patch("app.services.state_store.scheduler_leader_acquire", mock_acquire),
            patch("app.services.scheduler._run_inactivity_check", mock_inactivity),
        ):
            try:
                await asyncio.wait_for(_scheduler_loop(), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        mock_inactivity.assert_not_awaited()


# ---------------------------------------------------------------------------
# scheduler_leader_acquire token semantics (supplemental)
# ---------------------------------------------------------------------------


class TestSchedulerLeaderAcquireTokenSemantics:

    async def test_acquire_stores_uuid_not_one(self):
        """The stored value is the UUID token, not the literal '1'."""
        mock_redis = AsyncMock()
        stored_value: list[str] = []

        async def fake_set(key, value, *, nx, ex):
            stored_value.append(value)
            return True  # SET NX success

        mock_redis.set = fake_set

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            token = await state_store.scheduler_leader_acquire(ttl_seconds=90)

        assert token is not None
        assert stored_value[0] == token  # value stored matches returned token
        assert stored_value[0] != "1"    # regression: no longer stores literal "1"

    async def test_acquire_different_tokens_each_call(self):
        """Each successful acquire returns a unique token (different UUID)."""
        tokens: list[str] = []

        async def fake_set(key, value, *, nx, ex):
            tokens.append(value)
            return True

        mock_redis = AsyncMock()
        mock_redis.set = fake_set

        with patch.object(state_store._rc, "get_redis", AsyncMock(return_value=mock_redis)):
            t1 = await state_store.scheduler_leader_acquire()
            t2 = await state_store.scheduler_leader_acquire()

        assert t1 != t2
        assert tokens[0] != tokens[1]
