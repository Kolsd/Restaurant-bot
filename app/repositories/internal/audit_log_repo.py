"""
Audit log repository — hq_audit_log table.

GLOBAL table (no RLS) used exclusively by internal Mesio tooling.
All functions must be called from within bypass_tenant_scope("...").

Best-effort writes: db_log_audit_event never raises — it logs the
error via structlog and returns 0 on failure so the calling action
is never blocked by audit infrastructure.
"""

from __future__ import annotations

import json
from typing import Optional

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessor — breaks circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(row: dict) -> dict:
    result = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()
        elif isinstance(v, dict) or isinstance(v, list):
            result[k] = v
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    return result


async def db_log_audit_event(
    actor: str,
    action: str,
    *,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    org_id: Optional[int] = None,
    payload: Optional[dict] = None,
    request_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> int:
    """Insert an audit log entry. Cross-tenant — call within bypass_tenant_scope.

    Best-effort: swallows DB errors and logs them via structlog.
    Returns the new row id on success, 0 on failure.
    """
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO hq_audit_log
                  (actor, action, target_type, target_id, org_id,
                   payload, request_ip, user_agent)
                VALUES ($1, $2, $3, $4, $5, CAST($6 AS jsonb), $7, $8)
                RETURNING id
                """,
                actor,
                action,
                target_type,
                target_id,
                org_id,
                json.dumps(payload or {}),
                request_ip,
                user_agent,
            )
            return int(row_id) if row_id else 0
    except Exception:
        log.exception(
            "audit_log_repo.write_failed",
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
        )
        return 0


async def db_get_audit_log(
    *,
    actor: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    org_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """List audit entries with optional filters. Cross-tenant. Sorted created_at DESC."""
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if actor is not None:
        conditions.append(f"actor = ${idx}")
        params.append(actor)
        idx += 1

    if target_type is not None:
        conditions.append(f"target_type = ${idx}")
        params.append(target_type)
        idx += 1

    if target_id is not None:
        conditions.append(f"target_id = ${idx}")
        params.append(target_id)
        idx += 1

    if org_id is not None:
        conditions.append(f"org_id = ${idx}")
        params.append(org_id)
        idx += 1

    params.append(limit)
    params.append(offset)

    where = " AND ".join(conditions)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, actor, action, target_type, target_id, org_id,
                   payload, request_ip, user_agent, created_at
            FROM hq_audit_log
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_count_audit_log(
    *,
    actor: Optional[str] = None,
    org_id: Optional[int] = None,
) -> int:
    """Count audit entries matching optional actor/org_id filters. Cross-tenant."""
    conditions = ["1=1"]
    params: list = []
    idx = 1

    if actor is not None:
        conditions.append(f"actor = ${idx}")
        params.append(actor)
        idx += 1

    if org_id is not None:
        conditions.append(f"org_id = ${idx}")
        params.append(org_id)
        idx += 1

    where = " AND ".join(conditions)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT COUNT(*) FROM hq_audit_log WHERE {where}",
            *params,
        )
    return int(count) if count else 0
