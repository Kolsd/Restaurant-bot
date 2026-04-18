"""
app/services/tenant_context.py

ContextVar-based tenant isolation primitives.

WHY: asyncpg connections are acquired per-request; we need a way to ensure
every DB call within a request is automatically scoped to the correct
org_id without threading the ID through every function signature.
ContextVars propagate into asyncio Tasks (copy-on-write), so each concurrent
request gets its own isolated tenant identity.

Wave 2 semantics (Bloque S6):
  - The ContextVar holds an org_id. Call sites that previously passed
    restaurant_id continue to work for single-location tenants because
    organizations.id == old restaurants.id for every Matriz (migration 0034).
  - tenant_scope() parameter is kept as-is for now; callers migrate to
    passing org_id explicitly in Bloque S7.
  - current_org_id() is the canonical accessor; get_current_tenant() and
    current_tenant_id() are aliases retained for backward compatibility.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator, Optional

from app.services.logging import get_logger

log = get_logger(__name__)

# ── ContextVars ───────────────────────────────────────────────────────────────

# Holds the org_id for the current async context (request/task).
# Named "mesio_current_tenant" for ContextVar identity stability — renaming
# would invalidate any snapshot/copy already in flight (e.g. running tasks).
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


def current_org_id() -> Optional[int]:
    """Return the active org_id for the current async context.

    This is the canonical Wave 2 accessor. The ContextVar holds an org_id
    (callers that previously passed restaurant_id continue to work for
    single-location tenants because organizations.id == old restaurants.id
    for every Matriz, guaranteed by migration 0034).

    Returns:
        None  — when bypass is active (cross-tenant admin operation).
        int   — the pinned org_id.

    Raises:
        TenantNotSetError — when neither tenant nor bypass is set.
    """
    return get_current_tenant()


def current_tenant_id() -> Optional[int]:
    """Alias for current_org_id() — retained for backward compatibility.

    Deprecated: new code should call current_org_id() directly. This alias
    will be removed in Bloque S7 once all call sites are updated.
    """
    return get_current_tenant()


def peek_tenant() -> Optional[int]:
    """Non-raising read of the current tenant — safe to use in logging/debugging."""
    return _current_tenant.get()


# ── Context managers ──────────────────────────────────────────────────────────

@contextmanager
def tenant_scope(org_id: int) -> Generator[int, None, None]:
    """Pin the current async context to *org_id*.

    Validates that org_id is a positive integer; raises ValueError otherwise
    so callers fail fast on bad data (e.g. None slipping through).

    Nested scopes are supported: the inner scope shadows the outer one and the
    outer value is restored on exit (ContextVar token reset).

    Wave 2 (Bloque S6): the parameter was restaurant_id in Wave 1; it is now
    org_id. Existing call sites that pass restaurant_id continue to work for
    single-location tenants because organizations.id == old restaurants.id for
    every Matriz (guaranteed by migration 0034). Multi-branch call sites must
    be updated to pass org_id explicitly (tracked in Bloque S7).

    The value pinned here is used by tenant_db.tenant_connection() to set
    app.org_id for the duration of the DB transaction.
    """
    if not isinstance(org_id, int) or isinstance(org_id, bool):
        raise ValueError(
            f"org_id must be a positive int, got {org_id!r}"
        )
    if org_id <= 0:
        raise ValueError(
            f"org_id must be > 0, got {org_id}"
        )

    token = _current_tenant.set(org_id)
    try:
        yield org_id
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
