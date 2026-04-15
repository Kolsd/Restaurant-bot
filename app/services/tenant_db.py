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


@asynccontextmanager
async def tenant_connection() -> AsyncGenerator[asyncpg.Connection, None]:
    """Acquire a transactional connection scoped to the current tenant.

    Behaviour:
    - Bypass active  → executes `SET LOCAL ROLE mesio_superadmin` (admin ops)
    - Tenant set     → executes `SELECT set_config('app.restaurant_id', $1, true)`
                       using a parameterised call — never an f-string.
    - Neither        → raises TenantNotSetError immediately (before touching DB).

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
    async with pool.acquire() as conn:
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


@asynccontextmanager
async def tenant_connection_readonly() -> AsyncGenerator[asyncpg.Connection, None]:
    """Like tenant_connection() but additionally sets the transaction read-only.

    Useful for analytics/reporting queries where accidental writes must be
    prevented at the DB level, not only by convention.
    """
    async with tenant_connection() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        yield conn
