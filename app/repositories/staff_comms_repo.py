"""
app/repositories/staff_comms_repo.py

Repository for admin→staff communication: announcements and task checklists.

All functions require an active tenant_scope (org_id) in the call site.
No bypass_tenant_scope needed — all three tables are single-tenant.

Tables:
  staff_announcements      — one-way broadcasts from admin.
  staff_tasks              — shift checklist items.
  staff_task_completions   — per-staff completion tracking.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


def _row_to_dict(row) -> dict:
    """Convert an asyncpg Record to a plain dict with serializable values."""
    if row is None:
        return {}
    d: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


# ── Announcements ─────────────────────────────────────────────────────────────

async def db_create_announcement(
    org_id: int,
    title: str,
    body: str,
    created_by: str | None,
    expires_at: datetime | None = None,
    published: bool = True,
) -> dict:
    """Insert a new announcement. Returns the created row as dict."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO staff_announcements
                (org_id, title, body, created_by, published_at, expires_at, active)
            VALUES
                ($1, $2, $3, $4, $5, $6, true)
            RETURNING *
            """,
            org_id,
            title,
            body,
            created_by,
            datetime.now(tz=timezone.utc) if published else None,
            expires_at,
        )
    return _row_to_dict(row)


async def db_list_announcements_admin(
    org_id: int,
    include_inactive: bool = False,
) -> list[dict]:
    """Return all announcements for admin management view.

    With include_inactive=False (default) returns only active rows.
    With include_inactive=True returns all rows (for audit/history).
    Ordered newest first.
    """
    async with tenant_connection() as conn:
        if include_inactive:
            rows = await conn.fetch(
                """
                SELECT * FROM staff_announcements
                WHERE org_id = $1
                ORDER BY created_at DESC
                """,
                org_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM staff_announcements
                WHERE org_id = $1 AND active = true
                ORDER BY created_at DESC
                """,
                org_id,
            )
    return [_row_to_dict(r) for r in rows]


async def db_list_announcements_active(org_id: int, now: datetime) -> list[dict]:
    """Return currently visible announcements for staff self-service.

    Filters: active=true, published_at IS NOT NULL, not yet expired.
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM staff_announcements
            WHERE org_id = $1
              AND active = true
              AND published_at IS NOT NULL
              AND (expires_at IS NULL OR expires_at > $2)
            ORDER BY created_at DESC
            """,
            org_id,
            now,
        )
    return [_row_to_dict(r) for r in rows]


async def db_soft_delete_announcement(org_id: int, announcement_id: int) -> bool:
    """Mark an announcement inactive. Returns True if found and updated."""
    async with tenant_connection() as conn:
        result = await conn.execute(
            """
            UPDATE staff_announcements
            SET active = false
            WHERE id = $1 AND org_id = $2
            """,
            announcement_id,
            org_id,
        )
    return result == "UPDATE 1"


# ── Tasks ─────────────────────────────────────────────────────────────────────

async def db_create_task(
    org_id: int,
    title: str,
    description: str,
    created_by: str | None,
    due_date=None,
) -> dict:
    """Insert a new task. Returns the created row as dict."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO staff_tasks
                (org_id, title, description, created_by, due_date, active)
            VALUES
                ($1, $2, $3, $4, $5, true)
            RETURNING *
            """,
            org_id,
            title,
            description,
            created_by,
            due_date,
        )
    return _row_to_dict(row)


async def db_list_tasks_admin(
    org_id: int,
    include_inactive: bool = False,
) -> list[dict]:
    """Return all tasks for admin management view (newest first).

    Each row is augmented with completions_count for the task list badge.
    """
    async with tenant_connection() as conn:
        if include_inactive:
            rows = await conn.fetch(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM staff_task_completions c WHERE c.task_id = t.id) AS completions_count
                FROM staff_tasks t
                WHERE t.org_id = $1
                ORDER BY t.created_at DESC
                """,
                org_id,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM staff_task_completions c WHERE c.task_id = t.id) AS completions_count
                FROM staff_tasks t
                WHERE t.org_id = $1 AND t.active = true
                ORDER BY t.created_at DESC
                """,
                org_id,
            )
    return [_row_to_dict(r) for r in rows]


async def db_list_tasks_active(org_id: int) -> list[dict]:
    """Return active tasks for staff self-service (newest first).

    Includes completions_count for UI badge.
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT t.*,
                   (SELECT COUNT(*) FROM staff_task_completions c WHERE c.task_id = t.id) AS completions_count
            FROM staff_tasks t
            WHERE t.org_id = $1 AND t.active = true
            ORDER BY t.created_at DESC
            """,
            org_id,
        )
    return [_row_to_dict(r) for r in rows]


async def db_soft_delete_task(org_id: int, task_id: int) -> bool:
    """Mark a task inactive. Returns True if found and updated."""
    async with tenant_connection() as conn:
        result = await conn.execute(
            """
            UPDATE staff_tasks
            SET active = false
            WHERE id = $1 AND org_id = $2
            """,
            task_id,
            org_id,
        )
    return result == "UPDATE 1"


# ── Task completions ──────────────────────────────────────────────────────────

async def db_complete_task(
    task_id: int,
    staff_id: str,
    org_id: int,
    notes: str | None = None,
) -> dict:
    """Record or update a task completion for a staff member.

    Uses INSERT ... ON CONFLICT DO UPDATE so calling this twice for the same
    (task_id, staff_id) pair updates notes and refreshes completed_at.
    Returns the upserted row.
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO staff_task_completions (task_id, staff_id, org_id, notes, completed_at)
            VALUES ($1, $2::uuid, $3, $4, NOW())
            ON CONFLICT (task_id, staff_id) DO UPDATE
                SET notes        = EXCLUDED.notes,
                    completed_at = NOW()
            RETURNING *
            """,
            task_id,
            staff_id,
            org_id,
            notes,
        )
    return _row_to_dict(row)


async def db_list_completions_for_task(task_id: int) -> list[dict]:
    """Return all completion records for a task (admin insight: who finished it)."""
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT c.*, s.name AS staff_name, s.role AS staff_role
            FROM staff_task_completions c
            JOIN staff s ON s.id = c.staff_id
            WHERE c.task_id = $1
            ORDER BY c.completed_at DESC
            """,
            task_id,
        )
    return [_row_to_dict(r) for r in rows]


async def db_get_completions_for_staff(
    staff_id: str,
    task_ids: list[int],
) -> dict[int, dict]:
    """Return a mapping of task_id → completion_dict for tasks this staff completed.

    Used by staff self-service to render the completed_by_me flag efficiently
    in a single round-trip regardless of task list length.

    Returns empty dict if task_ids is empty.
    """
    if not task_ids:
        return {}
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM staff_task_completions
            WHERE staff_id = $1::uuid AND task_id = ANY($2::bigint[])
            """,
            staff_id,
            task_ids,
        )
    return {r["task_id"]: _row_to_dict(r) for r in rows}
