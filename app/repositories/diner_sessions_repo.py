"""
app/repositories/diner_sessions_repo.py
========================================
Repository for anonymous diner web-chat sessions (Mesio-native chat channel).

A diner session binds a synthetic identity token ("web:<uuid4>") to
(org_id, location_id, table_id | order_mode). The token IS the `phone`
value threaded through every existing phone-keyed function — agent.chat(),
carts, conversations, NPS, waiter_alerts — per the platform-wide convention
that "phone" is an opaque identity string, not a validated number.

Scope notes (mirrors qr_claims_repo.py — see that file's docstring):
  - create() runs WITHIN tenant_scope(org_id). The org is already known at
    session-creation time because the caller resolved it from the table or
    the org slug BEFORE calling this function.
  - get_by_token() runs from the pre-tenant resolution path: a diner's
    request only carries the token, not the org_id, so the tenant cannot be
    known yet. It uses bypass_tenant_scope — that lookup IS the tenant
    resolution itself (same pattern as qr_claims_repo.find_unclaimed_by_phone).
  - touch_last_seen() / set_contact_info() run AFTER resolution, inside the
    caller's tenant_scope(org_id).
"""

from __future__ import annotations

from typing import Optional

from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


async def create_session(
    token: str,
    org_id: int,
    location_id: Optional[int] = None,
    table_id: Optional[str] = None,
    table_name: Optional[str] = None,
    order_mode: str = "dine_in",
) -> dict:
    """Insert a new diner session row and return it.

    # Requires active tenant_scope(org_id) — org must already be resolved.
    """
    if not token or not org_id:
        raise ValueError("token and org_id are required")
    if order_mode not in ("dine_in", "delivery", "pickup"):
        raise ValueError(f"invalid order_mode: {order_mode!r}")

    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO diner_sessions
                (token, org_id, location_id, table_id, table_name,
                 order_mode)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, token, org_id, location_id, table_id, table_name,
                      order_mode, phone, display_name,
                      created_at, last_seen_at
            """,
            token, org_id, location_id, table_id, table_name,
            order_mode,
        )
    log.info(
        "diner_session.created",
        org_id=org_id,
        location_id=location_id,
        table_id=table_id,
        order_mode=order_mode,
    )
    return dict(row)


async def get_by_token(token: str) -> Optional[dict]:
    """Look up a diner session by its token.

    Pre-tenant lookup — the caller does not know org_id yet (that's what
    this function resolves). Runs under bypass_tenant_scope, mirroring
    qr_claims_repo.find_unclaimed_by_phone. Returns None if not found.
    """
    if not token:
        return None
    with bypass_tenant_scope("diner_session_token_lookup"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, token, org_id, location_id, table_id, table_name,
                       order_mode, phone, display_name,
                       created_at, last_seen_at
                FROM diner_sessions
                WHERE token = $1
                """,
                token,
            )
    return dict(row) if row else None


async def touch_last_seen(token: str, org_id: int) -> None:
    """Update last_seen_at for a session. Runs inside the caller's tenant_scope(org_id)."""
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE diner_sessions SET last_seen_at = NOW() WHERE token = $1",
            token,
        )


async def set_contact_info(
    token: str,
    org_id: int,
    phone: Optional[str] = None,
    display_name: Optional[str] = None,
) -> Optional[dict]:
    """Capture OPTIONAL phone/name at payment time. Runs inside tenant_scope(org_id).

    Only overwrites fields that are provided (non-None). Returns the updated
    row, or None if the token doesn't exist in this tenant scope.
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE diner_sessions
            SET phone = COALESCE($2, phone),
                display_name = COALESCE($3, display_name),
                last_seen_at = NOW()
            WHERE token = $1
            RETURNING id, token, org_id, location_id, table_id, table_name,
                      order_mode, phone, display_name,
                      created_at, last_seen_at
            """,
            token, phone, display_name,
        )
    return dict(row) if row else None
