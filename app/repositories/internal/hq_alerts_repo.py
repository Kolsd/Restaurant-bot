"""
hq_alerts — Mesio HQ alerts that open, stay and resolve (migration 0109).

Written by services/hq_alerts.run_alert_rules (scheduler), read by Mesio HQ.
Callers MUST be inside bypass_tenant_scope.
"""
from __future__ import annotations

from app.services.tenant_db import tenant_connection

# The live demo self-recycles tables and orders all day: never alert on it.
_EXCLUDED_SLUGS = ("casa-mesio-demo",)


async def db_org_ids_to_watch() -> list[int]:
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            "SELECT id FROM organizations WHERE COALESCE(slug, '') <> ALL($1::text[]) ORDER BY id",
            list(_EXCLUDED_SLUGS),
        )
    return [int(r["id"]) for r in rows]


async def db_upsert_open(*, key: str, code: str, severity: str, org_id: int | None,
                         location_id: int | None, title: str, detail: str | None,
                         count: int | None) -> dict:
    """Open the alert, or refresh the one already open for `key`.
    Returns {id, opened (bool: created now), emailed_at}."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """INSERT INTO hq_alerts (key, code, severity, org_id, location_id, title, detail, count)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (key) WHERE status = 'open'
               DO UPDATE SET last_seen_at = NOW(), severity = EXCLUDED.severity,
                             title = EXCLUDED.title, detail = EXCLUDED.detail, count = EXCLUDED.count
               RETURNING id, (xmax = 0) AS opened, emailed_at""",
            key, code, severity, org_id, location_id, title, detail, count,
        )
    return dict(row)


async def db_resolve_missing(org_ids: list[int], seen_keys: list[str]) -> int:
    """Resolve open alerts of the organizations evaluated in this run whose
    key was not seen again. Orgs that failed to evaluate keep theirs."""
    if not org_ids:
        return 0
    async with tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE hq_alerts SET status = 'resolved', resolved_at = NOW()
                WHERE status = 'open' AND org_id = ANY($1::bigint[]) AND key <> ALL($2::text[])""",
            org_ids, seen_keys,
        )
    return int(result.split()[-1])


async def db_mark_emailed(ids: list[int]) -> None:
    if not ids:
        return
    async with tenant_connection() as conn:
        await conn.execute("UPDATE hq_alerts SET emailed_at = NOW() WHERE id = ANY($1::bigint[])", ids)


async def db_list_alerts(*, status: str | None, org_id: int | None, limit: int = 100) -> list[dict]:
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT a.*, o.name AS org_name, l.name AS location_name
                 FROM hq_alerts a
                 LEFT JOIN organizations o ON o.id = a.org_id
                 LEFT JOIN locations l ON l.id = a.location_id AND l.org_id = a.org_id
                WHERE ($1::text IS NULL OR a.status = $1)
                  AND ($2::bigint IS NULL OR a.org_id = $2)
                ORDER BY (a.status = 'open') DESC,
                         CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                         a.opened_at DESC
                LIMIT $3""",
            status, org_id, limit,
        )
    return [dict(r) for r in rows]
