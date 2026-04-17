"""
app/services/tenant_context.py

ContextVar-based tenant isolation primitives.

WHY: asyncpg connections are acquired per-request; we need a way to ensure
every DB call within a request is automatically scoped to the correct
restaurant_id without threading the ID through every function signature.
ContextVars propagate into asyncio Tasks (copy-on-write), so each concurrent
request gets its own isolated tenant identity.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator, Optional

from app.services.logging import get_logger

log = get_logger(__name__)

# ── ContextVars ───────────────────────────────────────────────────────────────

# Holds the restaurant_id for the current async context (request/task).
_current_tenant: ContextVar[Optional[int]] = ContextVar(
    "mesio_current_tenant", default=None
)

# When True, tenant checks are bypassed (admin/internal operations).
_bypass_flag: ContextVar[bool] = ContextVar(
    "mesio_bypass_flag", default=False
)

# Human-readable reason required whenever bypass is used; aids audit logs.
_bypass_reason: ContextVar[Optional[str]] = ContextVar(
    "mesio_bypass_reason", default=None
)


# ── Exceptions ────────────────────────────────────────────────────────────────

class TenantNotSetError(RuntimeError):
    """Raised when a DB call is attempted with no tenant context and no bypass."""


class TenantContextConflict(RuntimeError):
    """Raised when bypass_tenant_scope is entered while a tenant is already pinned.

    Mixing a pinned tenant with a bypass in the same coroutine is almost always
    a programming error — the caller should exit the tenant_scope first.
    """


# ── Public accessors ──────────────────────────────────────────────────────────

def get_current_tenant() -> Optional[int]:
    """Return the active tenant id.

    Returns:
        None  — when bypass is active (cross-tenant admin operation).
        int   — the pinned restaurant_id.

    Raises:
        TenantNotSetError — when neither tenant nor bypass is set.
    """
    if _bypass_flag.get():
        return None
    tid = _current_tenant.get()
    if tid is None:
        raise TenantNotSetError(
            "No tenant context is set. Wrap this call in tenant_scope() "
            "or bypass_tenant_scope()."
        )
    return tid


def peek_tenant() -> Optional[int]:
    """Non-raising read of the current tenant — safe to use in logging/debugging."""
    return _current_tenant.get()


# ── Context managers ──────────────────────────────────────────────────────────

@contextmanager
def tenant_scope(restaurant_id: int) -> Generator[int, None, None]:
    """Pin the current async context to *restaurant_id*.

    Validates that restaurant_id is a positive integer; raises ValueError
    otherwise so callers fail fast on bad data (e.g. None slipping through).

    Nested scopes are supported: the inner scope shadows the outer one and the
    outer value is restored on exit (ContextVar token reset).
    """
    if not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool):
        raise ValueError(
            f"restaurant_id must be a positive int, got {restaurant_id!r}"
        )
    if restaurant_id <= 0:
        raise ValueError(
            f"restaurant_id must be > 0, got {restaurant_id}"
        )

    token = _current_tenant.set(restaurant_id)
    try:
        yield restaurant_id
    finally:
        _current_tenant.reset(token)


@contextmanager
def bypass_tenant_scope(reason: str) -> Generator[None, None, None]:
    """Temporarily disable tenant isolation for admin/internal operations.

    Args:
        reason: Mandatory audit string describing WHY the bypass is needed.
                Must be at least 8 characters.

    Raises:
        ValueError              — if reason is too short.
        TenantContextConflict   — if a tenant is already pinned in this context.
    """
    if len(reason) < 8:
        raise ValueError(
            f"bypass_tenant_scope reason must be >= 8 chars, got {reason!r}"
        )
    if _current_tenant.get() is not None:
        raise TenantContextConflict(
            f"Cannot enter bypass_tenant_scope while tenant "
            f"{_current_tenant.get()} is pinned. Exit tenant_scope first."
        )

    log.debug("tenant.bypass.enter", reason=reason)
    flag_token = _bypass_flag.set(True)
    reason_token = _bypass_reason.set(reason)
    try:
        yield
    finally:
        _bypass_flag.reset(flag_token)
        _bypass_reason.reset(reason_token)
        log.debug("tenant.bypass.exit", reason=reason)


@contextmanager
def bypass_tenant_scope_if_unset(reason: str) -> Generator[None, None, None]:
    """Enter bypass only if no tenant is currently pinned; no-op otherwise.

    Why this exists: functions like `agent.detect_table_context` were written
    in the pre-RLS era when the bot runtime had no active tenant scope, so
    they used `bypass_tenant_scope` to do pre-tenant lookups by `bot_number`.
    After Phase 1, `inbox_worker._handle_meta_whatsapp` wraps the whole bot
    flow in `tenant_scope(rid)` — now those same functions are called with an
    active scope, and a nested `bypass_tenant_scope` would raise
    TenantContextConflict.

    This helper lets shared helpers work in both regimes:
    - Active tenant scope (production via inbox_worker): no-op; the existing
      scope is honored. Reads go through RLS for that tenant, which is
      correct — the lookup by bot_number is within-tenant once the tenant is
      known.
    - No tenant scope (legacy `/chat` endpoint, Twilio webhook, tests): real
      bypass is entered; legacy cross-tenant behavior is preserved.

    Do NOT use this as a default — reserve for the handful of agent.py
    helpers that predate Phase 1. For genuinely cross-tenant operations
    (internal/admin, scheduler leader tick, inbox pre-resolution), keep using
    `bypass_tenant_scope` which fails loudly if called inside a scope.
    """
    if _current_tenant.get() is not None:
        yield
        return
    with bypass_tenant_scope(reason):
        yield
