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


def _canonical_phone(raw: str) -> str:
    """Canonicalize a phone to digits-only WITH country code, matching the
    format Meta WhatsApp webhooks deliver (e.g. '573144914554').

    Both sides of the QR-Phone-Claim flow MUST agree on this canonical form
    or the lookup fails:
      - The web menu modal (catalog-v2.js → POST /api/qr-claim) — customer
        may type the number with or without country code, with or without
        '+', with or without spaces/dashes.
      - The bot inbox path (Meta webhook → find_unclaimed_by_phone) —
        Meta always sends digits-only with country code prefixed.

    Rules (Colombia-only assumption — Mesio is CO-only today):
      - Strip all non-digit characters (spaces, '+', '-', '(', ')', etc.).
      - 10 digits starting with '3' → mobile CO without country code →
        prepend '57' → e.g. '3144914554' becomes '573144914554'.
      - 11 digits starting with '57' followed by a non-3 digit (e.g. '5712345678')
        → treat as already canonical (CO landline format).
      - 12+ digits → assume already canonical (any country), return as-is.
      - <7 digits → invalid, return ''.

    TODO (international expansion): once Mesio supports non-CO tenants,
    pass the org's default country code (or detect from bot_number prefix)
    so we don't hard-code '57'. For now, the assumption is safe because
    every restaurant on the platform is Colombian.
    """
    if not raw:
        return ""
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) < 7:
        return ""
    if len(digits) == 10 and digits.startswith("3"):
        # CO mobile without country code → prepend 57
        return "57" + digits
    if len(digits) == 11 and digits.startswith("57") and not digits.startswith("573"):
        # CO landline already with country code (e.g. 5712345678)
        return digits
    # 11+ digits otherwise (12, 13, 14, 15) → assume already canonical
    return digits


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
    norm_phone = _canonical_phone(phone)
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
    norm_phone = _canonical_phone(phone)
    if not norm_phone or not bot_number:
        return None

    # Try canonical form first (what _canonical_phone produces). Fall back to
    # the raw local form (10-digit Colombia mobile without country code) for
    # any legacy claims stored before the canonicalization fix in d7ceacc
    # (2026-04-28). Within 10 min those legacy rows expire; this fallback
    # bridges the gap without losing in-flight claims.
    candidates = [norm_phone]
    if norm_phone.startswith("57") and len(norm_phone) == 12:
        candidates.append(norm_phone[2:])  # strip CO country code → 10 digits

    with bypass_tenant_scope_if_unset("qr_claims_repo.find_unclaimed_by_phone: pre-tenant lookup"):
        async with tenant_connection() as conn:
            for candidate in candidates:
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
                    candidate, bot_number,
                )
                if row:
                    return dict(row)
    return None


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


async def count_pending_for_bot(bot_number: str, minutes: int = 5) -> int:
    """Count unconsumed claims for a bot in the last N minutes.

    Used as a soft signal for DISCONNECT #1 (Bot↔Mesa items huérfanos):
    when detect_table_context Path 0 fails to find a claim by phone but
    a claim DOES exist for the same restaurant (likely customer typed
    a different phone in the modal than the one they used to send the
    WhatsApp message), we log a structured warning so the admin can
    spot and manually link the orphaned delivery order to the mesa.

    Returns 0 if bot_number is empty or no recent pending claims.
    """
    if not bot_number:
        return 0
    with bypass_tenant_scope_if_unset("qr_claims_repo.count_pending_for_bot: pre-tenant lookup"):
        async with tenant_connection() as conn:
            row = await conn.fetchval(
                """
                SELECT COUNT(*) FROM qr_scan_pending
                WHERE bot_number = $1
                  AND claimed_at IS NULL
                  AND expires_at > NOW()
                  AND created_at > NOW() - ($2::int * INTERVAL '1 minute')
                """,
                bot_number, minutes,
            )
    return int(row or 0)


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
