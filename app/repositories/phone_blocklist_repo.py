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

    Implementation note: the original design used a partial UNIQUE index
    `WHERE blocked_until > NOW()` + ON CONFLICT UPSERT to enforce "one
    active block per phone". Postgres rejects partial-index predicates
    that reference NOW() (must be IMMUTABLE), so the schema uses a plain
    (non-unique) (phone, blocked_until DESC) index instead. We replicate
    the upsert semantics in app code: if there's an active block for this
    phone, UPDATE it; otherwise INSERT a fresh row. cleanup_expired prunes
    history so the table doesn't grow unbounded.

    Returns the row id.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _conn() as conn:
        async with conn.transaction():
            # Try to UPDATE the most recent active block for this phone.
            existing = await conn.fetchrow(
                """
                UPDATE phone_blocklist
                SET blocked_until = NOW() + ($1 || ' hours')::INTERVAL,
                    reason        = $2,
                    blocked_by    = $3,
                    org_id        = $4
                WHERE id = (
                    SELECT id FROM phone_blocklist
                    WHERE phone = $5 AND blocked_until > NOW()
                    ORDER BY blocked_until DESC
                    LIMIT 1
                    FOR UPDATE
                )
                RETURNING id
                """,
                str(hours), reason, blocked_by, org_id, phone,
            )
            if existing is not None:
                row_id = existing["id"]
            else:
                inserted = await conn.fetchrow(
                    """
                    INSERT INTO phone_blocklist (phone, org_id, reason, blocked_by, blocked_until)
                    VALUES ($1, $2, $3, $4, NOW() + ($5 || ' hours')::INTERVAL)
                    RETURNING id
                    """,
                    phone, org_id, reason, blocked_by, str(hours),
                )
                row_id = inserted["id"]
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


async def list_active_blocks_for_org(org_id: int, limit: int = 100) -> list[dict]:
    """List currently-active blocks (blocked_until > NOW()) for the given org.

    Returns a list of dicts with phone obfuscated (last 4 digits + ***) for
    safer display. Caller must already be inside tenant_scope() — the RLS
    policy ensures org_id matches even though we filter explicitly.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    safe_limit = max(1, min(limit, 500))
    async with _conn() as conn:
        rows = await conn.fetch(
            """
            SELECT id, phone, reason, blocked_by, blocked_until, created_at
            FROM phone_blocklist
            WHERE org_id = $1 AND blocked_until > NOW()
            ORDER BY blocked_until DESC
            LIMIT $2
            """,
            org_id, safe_limit,
        )
    out: list[dict] = []
    for r in rows:
        phone = r["phone"] or ""
        phone_obf = ("***" + phone[-4:]) if len(phone) >= 4 else "***"
        blocked_until = r["blocked_until"]
        created_at = r["created_at"]
        out.append({
            "id": r["id"],
            "phone": phone,
            "phone_obf": phone_obf,
            "reason": r["reason"] or "",
            "blocked_by": r["blocked_by"] or "",
            "blocked_until": blocked_until.isoformat() if isinstance(blocked_until, datetime) else None,
            "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
        })
    return out


async def remove_from_blocklist(phone: str, org_id: int) -> bool:
    """Remove all active blocks for *phone* belonging to *org_id*.

    Returns True if at least one row was deleted, False otherwise.
    Idempotent on repeat calls (second call returns False).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with _conn() as conn:
        result = await conn.execute(
            """
            DELETE FROM phone_blocklist
            WHERE phone = $1 AND org_id = $2 AND blocked_until > NOW()
            """,
            phone, org_id,
        )
    # asyncpg returns "DELETE N" — parse N safely.
    try:
        deleted_count = int(result.split()[-1])
    except (ValueError, AttributeError, IndexError):
        deleted_count = 0
    if deleted_count > 0:
        log.info(
            "phone_blocklist.removed",
            phone=phone,
            org_id=org_id,
            deleted=deleted_count,
        )
    return deleted_count > 0


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
