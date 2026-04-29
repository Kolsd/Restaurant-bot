"""
Phone blocklist for Capa 3 anti-impostor and abuse prevention.

Functions:
  add_to_blocklist   — block a phone for N hours (ON CONFLICT upsert)
  is_phone_blocked   — cross-tenant check: is this phone currently blocked?
  cleanup_expired    — scheduler task: delete old expired rows

Tenant notes:
  add_to_blocklist  → call within tenant_scope() or bypass_tenant_scope().
  is_phone_blocked  → MUST be called with bypass_tenant_scope() because a
                      phone blocked by any org should be detected everywhere.
  cleanup_expired   → MUST be called with bypass_tenant_scope() (cross-org).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


def _conn():
    """Lazy import tenant_connection to break import cycles."""
    from app.services.tenant_db import tenant_connection
    return tenant_connection()


async def add_to_blocklist(
    phone: str,
    org_id: int,
    reason: str,
    blocked_by: str,
    hours: int = 24,
) -> int:
    """Block *phone* for *hours* hours.

    Uses ON CONFLICT ... DO UPDATE so that if the phone is already blocked we
    simply extend / refresh the block with the new reason and expiry.  The
    unique partial index ux_phone_blocklist_active only covers rows where
    blocked_until > NOW(), so an expired row will not conflict — a fresh row
    is inserted instead.

    Returns the row id.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _conn() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
            VALUES ($1, $2, $3, $4, NOW() + ($5 || ' hours')::INTERVAL)
            ON CONFLICT (phone) WHERE blocked_until > NOW()
            DO UPDATE SET
                blocked_until = NOW() + ($5 || ' hours')::INTERVAL,
                reason        = EXCLUDED.reason,
                blocked_by    = EXCLUDED.blocked_by,
                org_id        = EXCLUDED.org_id
            RETURNING id
            """,
            phone,
            org_id,
            reason,
            blocked_by,
            str(hours),
        )
        row_id: int = row["id"]
        log.info(
            "phone_blocklist.added",
            phone=phone,
            org_id=org_id,
            reason=reason,
            hours=hours,
            row_id=row_id,
        )
        return row_id


async def is_phone_blocked(phone: str) -> dict | None:
    """Return the active block row for *phone* or None.

    Cross-tenant: a phone blocked by ANY org is flagged everywhere.
    This MUST be called inside bypass_tenant_scope so that the RLS
    org_isolation policy does not filter out blocks from other orgs.

    # Requires bypass_tenant_scope().
    """
    async with _conn() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, phone, org_id, reason, blocked_by, blocked_until, created_at
            FROM phone_blocklist
            WHERE phone = $1
              AND blocked_until > NOW()
            ORDER BY blocked_until DESC
            LIMIT 1
            """,
            phone,
        )
        if row is None:
            return None
        d = dict(row)
        if isinstance(d.get("blocked_until"), datetime):
            d["blocked_until"] = d["blocked_until"].isoformat()
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        return d


async def cleanup_expired(older_than_hours: int = 24) -> int:
    """Delete phone_blocklist rows where blocked_until is in the past by at
    least *older_than_hours* hours.  Idempotent and safe to run repeatedly.

    Returns the number of rows deleted.

    # Requires bypass_tenant_scope() (deletes across all orgs).
    """
    async with _conn() as conn:
        deleted = await conn.fetchval(
            """
            WITH del AS (
                DELETE FROM phone_blocklist
                WHERE blocked_until < NOW() - ($1 || ' hours')::INTERVAL
                RETURNING id
            )
            SELECT COUNT(*) FROM del
            """,
            str(older_than_hours),
        )
        if deleted:
            log.info("phone_blocklist.cleanup", deleted=deleted, older_than_hours=older_than_hours)
        return int(deleted or 0)
