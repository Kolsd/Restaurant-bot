"""
platform_errors — Mesio-internal error log (migration 0108).

Written by app/services/error_log.py, read by Mesio HQ. Callers MUST be
inside bypass_tenant_scope: the table belongs to no restaurant.
"""
from __future__ import annotations

from app.services.tenant_db import tenant_connection


async def db_insert_error(*, source: str, org_id: int | None, location_id: int | None,
                          route: str | None, method: str | None, status: int | None,
                          error_type: str, message: str | None, fingerprint: str,
                          request_id: str | None) -> None:
    async with tenant_connection() as conn:
        await conn.execute(
            """INSERT INTO platform_errors
                   (source, org_id, location_id, route, method, status, error_type,
                    message, fingerprint, request_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            source, org_id, location_id, route, method, status, error_type,
            message, fingerprint, request_id,
        )


async def db_error_groups(*, org_id: int | None, days: int, limit: int = 50) -> list[dict]:
    """Repeats of the same failure collapsed into one row, newest first.
    org_id=None → every organization (and errors with no org)."""
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT fingerprint,
                      MAX(source)      AS source,
                      MAX(route)       AS route,
                      MAX(method)      AS method,
                      MAX(status)      AS status,
                      MAX(error_type)  AS error_type,
                      (ARRAY_AGG(message ORDER BY created_at DESC))[1]    AS message,
                      (ARRAY_AGG(request_id ORDER BY created_at DESC))[1] AS last_request_id,
                      (ARRAY_AGG(location_id ORDER BY created_at DESC))[1] AS location_id,
                      org_id,
                      COUNT(*)::int    AS count,
                      MIN(created_at)  AS first_at,
                      MAX(created_at)  AS last_at
                 FROM platform_errors
                WHERE created_at >= NOW() - make_interval(days => $2)
                  AND ($1::bigint IS NULL OR org_id = $1)
                GROUP BY fingerprint, org_id
                ORDER BY MAX(created_at) DESC
                LIMIT $3""",
            org_id, days, limit,
        )
    return [dict(r) for r in rows]


async def db_error_counts_by_org(*, hours: int) -> list[dict]:
    """Errors per organization in the last `hours` (org_id NULL = platform)."""
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT e.org_id, o.name AS org_name, COUNT(*)::int AS count,
                      COUNT(*) FILTER (WHERE e.source = 'bot')::int AS bot_count,
                      MAX(e.created_at) AS last_at
                 FROM platform_errors e
                 LEFT JOIN organizations o ON o.id = e.org_id
                WHERE e.created_at >= NOW() - make_interval(hours => $1)
                GROUP BY e.org_id, o.name
                ORDER BY COUNT(*) DESC""",
            hours,
        )
    return [dict(r) for r in rows]


async def db_purge_errors(*, older_than_days: int = 90) -> int:
    async with tenant_connection() as conn:
        result = await conn.execute(
            "DELETE FROM platform_errors WHERE created_at < NOW() - make_interval(days => $1)",
            older_than_days,
        )
    return int(result.split()[-1]) if result else 0
