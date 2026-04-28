"""
app/repositories/qr_claims_repo.py
==================================
Repository for the QR-Phone-Claim flow (see docs/MESA_QR_ARCHITECTURE.md).

A "claim" is a pending pre-binding between a customer's phone and a table
QR scan, recorded by the menu-page modal BEFORE the customer messages
the bot. When the bot receives the message, it looks up the claim by
exact phone match and opens a session for that specific table.

Scope notes:
  - create_claim runs WITHIN tenant_scope (the org is known at QR scan
    time because the bot_number resolves it).
  - find_unclaimed_by_phone runs from the bot's pre-tenant resolution
    path (bot doesn't know which org the message belongs to until the
    claim is found). It uses bypass_tenant_scope internally because
    that lookup IS the tenant resolution itself.
  - mark_claimed runs after find — same tenant context (claim's org).
  - cleanup_expired runs from the scheduler under bypass_tenant_scope,
    cross-tenant by design.
"""

from __future__ import annotations

from typing import Optional

from app.services.logging import get_logger
from app.services.tenant_context import (
    bypass_tenant_scope,
    bypass_tenant_scope_if_unset,
)
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


def _normalize_phone(raw: str) -> str:
    """Digits only, no '+'. Matches the format used by Meta webhooks."""
    if not raw:
        return ""
    return str(raw).replace(" ", "").replace("+", "").replace("-", "").strip()


async def create_claim(
    bot_number: str,
    table_id: str,
    phone: str,
    org_id: int,
    location_id: Optional[int] = None,
    geo_verified: Optional[bool] = None,
    ttl_minutes: int = 10,
) -> int:
    """Insert a new claim and return its id.

    If the same (bot_number, phone) already has an unclaimed pending claim,
    it gets superseded — the new row wins on the bot's lookup because the
    repo lookup picks the most recent one by id DESC. Old rows expire
    naturally and the cleanup scheduler removes them.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    norm_phone = _normalize_phone(phone)
    if not norm_phone or not bot_number or not table_id:
        raise ValueError("bot_number, table_id, and phone are all required")

    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO qr_scan_pending
                (bot_number, org_id, location_id, table_id, phone,
                 geo_verified, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, NOW() + ($7::int * INTERVAL '1 minute'))
            RETURNING id
            """,
            bot_number, org_id, location_id, table_id, norm_phone,
            geo_verified, ttl_minutes,
        )
    log.info(
        "qr_claim.created",
        claim_id=int(row["id"]),
        org_id=org_id,
        location_id=location_id,
        table_id=table_id,
        bot_number=bot_number,
        geo_verified=geo_verified,
    )
    return int(row["id"])


async def find_unclaimed_by_phone(phone: str, bot_number: str) -> Optional[dict]:
    """Look up the most recent unclaimed, unexpired claim for this phone+bot.

    Soft-scope contract:
      - Production: called from agent.detect_table_context which runs inside
        tenant_scope(org_id) set by inbox_worker (Rule #14). The lookup
        uses the active scope — RLS filters to that org, which is correct
        because qr_scan_pending was created by /api/qr-claim under the
        SAME org (resolved from the same bot_number).
      - Tests / legacy /chat endpoint: called without a scope. The soft
        bypass enters bypass mode so the lookup works.

    A strict bypass_tenant_scope here would conflict with the active
    tenant_scope in production (TenantContextConflict). Diagnosed in
    deploy 2f91b58f-963c-41e1-8368-d12ce16ef60a (2026-04-28).

    Returns None if no matching claim exists.
    """
    norm_phone = _normalize_phone(phone)
    if not norm_phone or not bot_number:
        return None

    with bypass_tenant_scope_if_unset("qr_claims_repo.find_unclaimed_by_phone: pre-tenant lookup"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, bot_number, org_id, location_id, table_id,
                       phone, geo_verified, created_at, expires_at, claimed_at
                FROM qr_scan_pending
                WHERE phone = $1
                  AND bot_number = $2
                  AND claimed_at IS NULL
                  AND expires_at > NOW()
                ORDER BY id DESC
                LIMIT 1
                """,
                norm_phone, bot_number,
            )
    return dict(row) if row else None


async def mark_claimed(claim_id: int) -> bool:
    """Mark a claim as consumed. Returns True if it transitioned from
    unclaimed → claimed; False if it was already claimed (race with
    another worker — rare but possible with parallel inbox workers).

    Same soft-scope contract as find_unclaimed_by_phone — works under
    an active tenant_scope (production) or without one (tests).
    """
    with bypass_tenant_scope_if_unset("qr_claims_repo.mark_claimed: cross-tenant claim consumption"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                """
                UPDATE qr_scan_pending
                SET claimed_at = NOW()
                WHERE id = $1 AND claimed_at IS NULL
                RETURNING id
                """,
                claim_id,
            )
    if row is not None:
        log.info("qr_claim.consumed", claim_id=claim_id)
        return True
    log.info("qr_claim.consume_race_lost", claim_id=claim_id)
    return False


async def cleanup_expired(older_than_hours: int = 1) -> int:
    """Delete claims that have been expired for at least N hours.

    Called by scheduler periodically (cross-tenant). Returns the count
    of rows deleted for observability.
    """
    with bypass_tenant_scope("qr_claims_repo.cleanup_expired: scheduler cross-tenant"):
        async with tenant_connection() as conn:
            result = await conn.execute(
                """
                DELETE FROM qr_scan_pending
                WHERE expires_at < NOW() - ($1::int * INTERVAL '1 hour')
                """,
                older_than_hours,
            )
    # asyncpg returns "DELETE N" — extract the count.
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count > 0:
        log.info("qr_claim.cleanup", deleted=count, older_than_hours=older_than_hours)
    return count
