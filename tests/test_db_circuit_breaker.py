"""Tests for the DB circuit breaker in app/services/database.py.

Covers:
- closed → open transition after N failures in window
- open → fail-fast (no wait) while within cooldown
- open → half_open transition after cooldown
- half_open success → closed + reset
- half_open failure → back to open
- get_circuit_state() observability
- concurrent callers under open state all fail fast (no thundering herd)
"""

import asyncio
import importlib

import asyncpg
import pytest


@pytest.fixture(autouse=True)
def reset_circuit_state(monkeypatch):
    """Reset module-level circuit state between tests."""
    from app.services import database

    database._db_circuit_state = "closed"
    database._db_circuit_fail_count = 0
    database._db_circuit_opened_at = None
    database._db_circuit_fail_times = []
    database._db_circuit_fail_log_at = 0.0
    database._pool = None
    yield
    database._db_circuit_state = "closed"
    database._db_circuit_fail_count = 0
    database._db_circuit_opened_at = None
    database._db_circuit_fail_times = []
    database._db_circuit_fail_log_at = 0.0
    database._pool = None


@pytest.mark.asyncio
async def test_closed_to_open_after_threshold_failures(monkeypatch):
    from app.services import database

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")

    async def always_fail(*args, **kwargs):
        raise asyncpg.exceptions.CannotConnectNowError("simulated")

    monkeypatch.setattr(asyncpg, "create_pool", always_fail)

    # Accumulate failures; some iterations raise the underlying asyncpg error,
    # later iterations raise DBCircuitOpen once the breaker trips.
    seen_circuit_open = False
    for _ in range(database._DB_CIRCUIT_FAIL_THRESHOLD + 2):
        try:
            await database.get_pool()
        except database.DBCircuitOpen:
            seen_circuit_open = True
        except asyncpg.exceptions.CannotConnectNowError:
            pass

    assert seen_circuit_open, "circuit should have opened and started failing fast"
    assert database._db_circuit_opened_at is not None


@pytest.mark.asyncio
async def test_open_fails_fast_within_cooldown(monkeypatch):
    from app.services import database

    # Force into open state
    database._db_circuit_state = "open"
    database._db_circuit_opened_at = __import__("time").monotonic()

    # Must raise DBCircuitOpen immediately, NOT call asyncpg
    called = {"count": 0}

    async def should_not_be_called(*args, **kwargs):
        called["count"] += 1

    monkeypatch.setattr(asyncpg, "create_pool", should_not_be_called)

    with pytest.raises(database.DBCircuitOpen):
        await database.get_pool()

    assert called["count"] == 0  # asyncpg never touched


@pytest.mark.asyncio
async def test_open_transitions_to_half_open_after_cooldown(monkeypatch):
    from app.services import database

    # Force into open state with an opened_at that's already past cooldown
    database._db_circuit_state = "open"
    past = __import__("time").monotonic() - database._DB_CIRCUIT_COOLDOWN_SECONDS - 1
    database._db_circuit_opened_at = past

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")

    class DummyPool:
        pass

    async def fake_create_pool(*args, **kwargs):
        return DummyPool()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    pool = await database.get_pool()
    # Successful probe → circuit should have transitioned half_open → closed
    assert database._db_circuit_state == "closed"
    assert database._db_circuit_fail_count == 0
    assert pool is not None


@pytest.mark.asyncio
async def test_half_open_failure_returns_to_open(monkeypatch):
    from app.services import database

    # Populate the sliding window with threshold-1 past failures so that a
    # single probe failure in half_open pushes us back to open.
    now = __import__("time").monotonic()
    database._db_circuit_fail_times = [now - 1.0] * (database._DB_CIRCUIT_FAIL_THRESHOLD - 1)
    database._db_circuit_state = "half_open"
    database._db_circuit_opened_at = now - 100

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/fake")

    async def always_fail(*args, **kwargs):
        raise asyncpg.exceptions.CannotConnectNowError("probe failed")

    monkeypatch.setattr(asyncpg, "create_pool", always_fail)

    with pytest.raises(asyncpg.exceptions.CannotConnectNowError):
        await database.get_pool()

    assert database._db_circuit_state == "open"
    assert database._db_circuit_opened_at is not None


@pytest.mark.asyncio
async def test_concurrent_callers_all_fail_fast_when_open(monkeypatch):
    from app.services import database

    database._db_circuit_state = "open"
    database._db_circuit_opened_at = __import__("time").monotonic()

    called = {"count": 0}

    async def should_not_be_called(*args, **kwargs):
        called["count"] += 1

    monkeypatch.setattr(asyncpg, "create_pool", should_not_be_called)

    async def caller():
        with pytest.raises(database.DBCircuitOpen):
            await database.get_pool()

    # 20 concurrent callers — none should reach asyncpg
    await asyncio.gather(*[caller() for _ in range(20)])
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_get_circuit_state_observability():
    from app.services import database

    state = database.get_circuit_state()
    assert state["state"] == "closed"
    assert state["fail_count"] == 0
    assert state["opened_at"] is None

    database._db_circuit_state = "open"
    database._db_circuit_fail_count = 5
    database._db_circuit_opened_at = 123.456

    state = database.get_circuit_state()
    assert state["state"] == "open"
    assert state["fail_count"] == 5
    assert state["opened_at"] == 123.456
