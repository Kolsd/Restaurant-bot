"""
app/repositories/staff_comms_repo.py

Repository for admin→staff communication: announcements, task checklists,
and shift swap requests.

All functions require an active tenant_scope (org_id) in the call site.
No bypass_tenant_scope needed — all tables are single-tenant.

Tables:
  staff_announcements      — one-way broadcasts from admin.
  staff_tasks              — shift checklist items.
  staff_task_completions   — per-staff completion tracking.
  shift_swap_requests      — staff↔staff shift swap lifecycle.

Shift swap status lifecycle:
  pending
    → target_accepted  (target accepts)
    → target_rejected  (target rejects)
    → withdrawn        (requester cancels)
  target_accepted
    → admin_approved   (admin approves + executes swap)
    → admin_rejected   (admin rejects with notes)
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


# ── Shift swap requests ───────────────────────────────────────────────────────

# Valid transitions: from_status → set(allowed_to_statuses)
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending":          {"target_accepted", "target_rejected", "withdrawn"},
    "target_accepted":  {"admin_approved", "admin_rejected"},
}


async def db_create_shift_swap(
    org_id: int,
    requester_staff_id: str,
    target_staff_id: str,
    shift_date,                      # date or "YYYY-MM-DD" string
    shift_start_time=None,
    shift_end_time=None,
    reason: str | None = None,
) -> dict:
    """Insert a new shift swap request in status='pending'. Returns the created row."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO shift_swap_requests
                (org_id, requester_staff_id, target_staff_id,
                 shift_date, shift_start_time, shift_end_time, reason, status)
            VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, 'pending')
            RETURNING *
            """,
            org_id,
            str(requester_staff_id),
            str(target_staff_id),
            shift_date,
            shift_start_time,
            shift_end_time,
            reason,
        )
    return _row_to_dict(row)


async def db_list_swaps_for_target(
    org_id: int,
    target_staff_id: str,
    status: str = "pending",
) -> list[dict]:
    """Return incoming swap requests where the caller is the target.

    Joins with staff table to include requester name.
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT s.*,
                   r.name AS requester_name,
                   t.name AS target_name
            FROM shift_swap_requests s
            JOIN staff r ON r.id = s.requester_staff_id
            JOIN staff t ON t.id = s.target_staff_id
            WHERE s.org_id = $1
              AND s.target_staff_id = $2::uuid
              AND s.status = $3
            ORDER BY s.created_at DESC
            """,
            org_id,
            str(target_staff_id),
            status,
        )
    return [_row_to_dict(r) for r in rows]


async def db_list_swaps_by_requester(
    org_id: int,
    requester_staff_id: str,
    limit: int = 20,
) -> list[dict]:
    """Return outbound swap requests created by the caller (newest first)."""
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT s.*,
                   r.name AS requester_name,
                   t.name AS target_name
            FROM shift_swap_requests s
            JOIN staff r ON r.id = s.requester_staff_id
            JOIN staff t ON t.id = s.target_staff_id
            WHERE s.org_id = $1
              AND s.requester_staff_id = $2::uuid
            ORDER BY s.created_at DESC
            LIMIT $3
            """,
            org_id,
            str(requester_staff_id),
            limit,
        )
    return [_row_to_dict(r) for r in rows]


async def db_list_swaps_admin(
    org_id: int,
    status: str | None = None,
) -> list[dict]:
    """Return all swap requests for the org (admin view).

    If status is provided, filter to that status only.
    Joins requester and target names.
    """
    async with tenant_connection() as conn:
        if status:
            rows = await conn.fetch(
                """
                SELECT s.*,
                       r.name AS requester_name,
                       t.name AS target_name
                FROM shift_swap_requests s
                JOIN staff r ON r.id = s.requester_staff_id
                JOIN staff t ON t.id = s.target_staff_id
                WHERE s.org_id = $1 AND s.status = $2
                ORDER BY s.created_at DESC
                """,
                org_id,
                status,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT s.*,
                       r.name AS requester_name,
                       t.name AS target_name
                FROM shift_swap_requests s
                JOIN staff r ON r.id = s.requester_staff_id
                JOIN staff t ON t.id = s.target_staff_id
                WHERE s.org_id = $1
                ORDER BY s.created_at DESC
                """,
                org_id,
            )
    return [_row_to_dict(r) for r in rows]


async def db_get_swap(org_id: int, swap_id: int) -> dict:
    """Fetch a single swap request by id within this org. Returns {} if not found."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.*,
                   r.name AS requester_name,
                   t.name AS target_name
            FROM shift_swap_requests s
            JOIN staff r ON r.id = s.requester_staff_id
            JOIN staff t ON t.id = s.target_staff_id
            WHERE s.id = $1 AND s.org_id = $2
            """,
            swap_id,
            org_id,
        )
    return _row_to_dict(row) if row else {}


async def db_update_swap_status(
    org_id: int,
    swap_id: int,
    new_status: str,
    decided_by: str | None = None,
    admin_notes: str | None = None,
) -> dict:
    """Transition a swap request to a new status.

    Validates the transition is allowed. Raises ValueError for invalid transitions
    or if the swap is not found.

    Allowed transitions:
        pending         → target_accepted | target_rejected | withdrawn
        target_accepted → admin_approved  | admin_rejected
    """
    swap = await db_get_swap(org_id, swap_id)
    if not swap:
        raise ValueError(f"Swap request {swap_id} not found in org {org_id}")

    current = swap["status"]
    allowed = _VALID_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {current!r} → {new_status!r}. "
            f"Allowed from {current!r}: {allowed or 'none'}"
        )

    now = datetime.now(tz=timezone.utc)
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE shift_swap_requests
            SET status     = $1,
                decided_by = COALESCE($2, decided_by),
                decided_at = $3,
                admin_notes= COALESCE($4, admin_notes),
                updated_at = $3
            WHERE id = $5 AND org_id = $6
            RETURNING *
            """,
            new_status,
            decided_by,
            now,
            admin_notes,
            swap_id,
            org_id,
        )
    return _row_to_dict(row)


async def db_execute_swap(org_id: int, swap_id: int) -> dict:
    """Physically swap the staff_shifts rows for the two staff on the swap date.

    Finds the open shift row (or most recent shift that covers shift_date) for
    each participant and swaps their staff_id values.

    If one (or both) participants have no staff_shifts row on that date, the
    physical swap is skipped but the function still returns success — the status
    transition was already recorded by db_update_swap_status.

    Returns:
        {
          "requester_shift_id": int | None,
          "target_shift_id":    int | None,
          "swapped":            bool,
          "swap": dict          # the swap request row
        }
    """
    swap = await db_get_swap(org_id, swap_id)
    if not swap:
        raise ValueError(f"Swap request {swap_id} not found")

    requester_id = str(swap["requester_staff_id"])
    target_id    = str(swap["target_staff_id"])
    shift_date   = swap["shift_date"]

    async with tenant_connection() as conn:
        # Find one shift per participant whose clock_in falls on shift_date
        req_shift = await conn.fetchrow(
            """
            SELECT id, staff_id FROM staff_shifts
            WHERE staff_id = $1::uuid
              AND org_id   = $2
              AND clock_in::date = $3::date
            ORDER BY clock_in DESC
            LIMIT 1
            """,
            requester_id,
            org_id,
            shift_date,
        )
        tgt_shift = await conn.fetchrow(
            """
            SELECT id, staff_id FROM staff_shifts
            WHERE staff_id = $1::uuid
              AND org_id   = $2
              AND clock_in::date = $3::date
            ORDER BY clock_in DESC
            LIMIT 1
            """,
            target_id,
            org_id,
            shift_date,
        )

        if req_shift and tgt_shift:
            # Swap staff_id on both rows atomically
            async with conn.transaction():
                await conn.execute(
                    "UPDATE staff_shifts SET staff_id = $1::uuid WHERE id = $2",
                    target_id, req_shift["id"],
                )
                await conn.execute(
                    "UPDATE staff_shifts SET staff_id = $1::uuid WHERE id = $2",
                    requester_id, tgt_shift["id"],
                )
            swapped = True
        else:
            swapped = False

    return {
        "requester_shift_id": req_shift["id"] if req_shift else None,
        "target_shift_id":    tgt_shift["id"] if tgt_shift else None,
        "swapped":            swapped,
        "swap":               swap,
    }
