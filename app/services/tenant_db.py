"""
app/services/tenant_db.py

Async context managers that acquire a DB connection and enforce tenant
isolation at the PostgreSQL session level.

WHY: By setting `app.restaurant_id` as a session-local GUC (or switching to a
restricted role for superadmin), future Row-Level Security policies can
filter every query automatically without requiring WHERE clauses scattered
across every repository function. This file wires the ContextVar from
tenant_context.py into the actual asyncpg connection lifecycle.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import asyncpg

from app.services.logging import get_logger
from app.services.tenant_context import (
    TenantNotSetError,
    _bypass_flag,
    _bypass_reason,
    _current_tenant,
)

log = get_logger(__name__)

# Maximum seconds to wait for a connection from the pool before giving up.
# Keeping this short (3 s) means a saturated pool causes a fast, typed failure
# rather than a long hang that blocks the webhook handler and delays the 200 ACK
# to Meta.  Under normal load pool.acquire() completes in < 10 ms.
POOL_ACQUIRE_TIMEOUT = 3.0


class PoolAcquireTimeout(ConnectionError):
    """Raised when pool.acquire() does not return within POOL_ACQUIRE_TIMEOUT seconds.

    Subclasses ConnectionError so callers can catch it alongside other DB
    connectivity errors with a single ``except ConnectionError`` branch, while
    still allowing precise handling with ``except PoolAcquireTimeout``.
    """


async def _await_acquire_ctx(ctx: object) -> asyncpg.Connection:
    """Coroutine wrapper around a PoolAcquireContext for asyncio.wait_for().

    asyncpg's PoolAcquireContext supports both:
      - ``async with pool.acquire() as conn``  → via __aenter__/__aexit__
      - ``await pool.acquire()``               → via __await__

    We use the __aenter__ form here because test mocks (conftest.make_pool,
    FakePool in test_station_routing, etc.) only wire up __aenter__/__aexit__.

    If the ctx is a plain coroutine (e.g. a test mock returning ``async def
    _hanging_acquire()``), it has no __aenter__ and we fall back to awaiting it
    directly — this covers the timeout simulation test case.

    The CALLER is responsible for calling ``acquire_ctx.__aexit__`` in a finally
    block to return the connection to the pool.
    """
    aenter = getattr(ctx, "__aenter__", None)
    if aenter is not None:
        return await aenter()
    # Plain coroutine fallback (e.g. hanging mock for timeout tests).
    return await ctx  # type: ignore[misc]


@asynccontextmanager
async def tenant_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a transactional connection scoped to the current tenant.

    Behaviour:
    - Bypass active  → executes `SET LOCAL ROLE mesio_superadmin` (admin ops)
    - Tenant set     → executes `SELECT set_config('app.restaurant_id', $1, true)`
                       using a parameterised call — never an f-string.
    - Neither        → raises TenantNotSetError immediately (before touching DB).
    - Pool exhausted → raises PoolAcquireTimeout after POOL_ACQUIRE_TIMEOUT seconds.

    The `conn.transaction()` context manager handles rollback on any exception,
    so callers do not need to catch asyncpg errors here.
    """
    from app.services.database import get_pool  # lazy import — breaks import cycle

    bypass = _bypass_flag.get()
    tenant_id = _current_tenant.get()

    if not bypass and tenant_id is None:
        raise TenantNotSetError(
            "tenant_connection() requires an active tenant_scope() "
            "or bypass_tenant_scope()."
        )

    pool = await get_pool()

    # Enforce a timeout on pool.acquire() to prevent the webhook handler from
    # hanging indefinitely when the pool is exhausted.  asyncio.wait_for wraps
    # a coroutine (_await_acquire_ctx), so it can cancel on timeout.
    acquire_ctx = pool.acquire()  # PoolAcquireContext — awaitable + async ctxmgr
    try:
        conn = await asyncio.wait_for(
            _await_acquire_ctx(acquire_ctx), timeout=POOL_ACQUIRE_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        log.error(
            "tenant_db.pool_timeout",
            timeout_seconds=POOL_ACQUIRE_TIMEOUT,
            bypass=bypass,
            tenant_id=tenant_id,
        )
        raise PoolAcquireTimeout(
            f"Could not acquire DB connection within {POOL_ACQUIRE_TIMEOUT}s "
            "(pool exhausted or DB unreachable)"
        ) from exc

    # Connection acquired — always release it on exit via __aexit__ (symmetric
    # with the __aenter__ call in _await_acquire_ctx).
    try:
        async with conn.transaction():
            if bypass:
                reason = _bypass_reason.get()
                log.debug(
                    "tenant_db.bypass_role",
                    role="mesio_superadmin",
                    reason=reason,
                )
                await conn.execute("SET LOCAL ROLE mesio_superadmin")
            else:
                # Parameter-safe: str(tenant_id) passed as $1, never interpolated.
                await conn.fetchval(
                    "SELECT set_config('app.restaurant_id', $1, true)",
                    str(tenant_id),
                )
            yield conn
    finally:
        # Call __aexit__ on the acquire context to return the connection to the
        # pool.  This is symmetric with __aenter__ called in _await_acquire_ctx.
        aexit_fn = getattr(acquire_ctx, "__aexit__", None)
        if aexit_fn is not None:
            await aexit_fn(None, None, None)


@asynccontextmanager
async def tenant_connection_readonly() -> AsyncGenerator[asyncpg.Connection, None]:
    """Like tenant_connection() but additionally sets the transaction read-only.

    Useful for analytics/reporting queries where accidental writes must be
    prevented at the DB level, not only by convention.
    """
    async with tenant_connection() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        yield conn
