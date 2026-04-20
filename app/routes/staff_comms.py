"""
app/routes/staff_comms.py

Admin↔Staff Communication endpoints.

Admin routes (owner / gerente / admin):
  POST   /api/staff/announcements              create announcement
  GET    /api/staff/announcements              list (admin view)
  DELETE /api/staff/announcements/{id}         soft-delete

  POST   /api/staff/tasks                      create task
  GET    /api/staff/tasks                      list with completions_count
  GET    /api/staff/tasks/{id}/completions     who completed this task
  DELETE /api/staff/tasks/{id}                 soft-delete

Staff self-service routes (JWT staff:<uuid>):
  GET    /api/staff/self/announcements         active announcements
  GET    /api/staff/self/tasks                 active tasks + completed_by_me
  POST   /api/staff/self/tasks/{id}/complete   mark complete
  GET    /api/staff/self/tips                  tips today + this week (Sprint Y)
  GET    /api/staff/self/upcoming-shifts       next N scheduled shifts (Sprint Y)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.repositories import staff_comms_repo, staff_repo
from app.routes.deps import get_current_restaurant_scoped, get_current_user_scoped
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/staff", tags=["staff-comms"])

_ADMIN_ROLES = {"owner", "admin", "gerente"}


def _require_admin_role(user_or_restaurant: dict) -> None:
    """Raise 403 if the resolved user does not hold an admin-level role."""
    role_str = user_or_restaurant.get("role", "")
    roles = {r.strip().lower() for r in role_str.split(",") if r.strip()}
    if not roles.intersection(_ADMIN_ROLES):
        raise HTTPException(
            status_code=403,
            detail="Solo owner, admin o gerente pueden gestionar anuncios y tareas.",
        )


# ── Pydantic models ──────────────────────────────────────────────────────────

class AnnouncementCreate(BaseModel):
    title:      str             = Field(..., min_length=1, max_length=200)
    body:       str             = Field(..., min_length=1)
    expires_at: datetime | None = None


class TaskCreate(BaseModel):
    title:       str            = Field(..., min_length=1, max_length=200)
    description: str            = Field("", max_length=2000)
    due_date:    str | None     = None  # "YYYY-MM-DD" or null


class TaskCompleteBody(BaseModel):
    notes: str | None = Field(None, max_length=500)


# ── Admin: Announcements ─────────────────────────────────────────────────────

@router.post("/announcements", status_code=201)
async def create_announcement(
    request: Request,
    body: AnnouncementCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Create a new announcement for this org (admin only)."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]

    # Resolve the admin username from the Authorization token (used as created_by).
    from app.routes.deps import get_current_user  # noqa: PLC0415
    user = await get_current_user(request)
    created_by = user.get("username")

    ann = await staff_comms_repo.db_create_announcement(
        org_id=org_id,
        title=body.title,
        body=body.body,
        created_by=created_by,
        expires_at=body.expires_at,
        published=True,
    )
    return {"announcement": ann}


@router.get("/announcements")
async def list_announcements_admin(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List all active announcements for admin management view."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]
    items = await staff_comms_repo.db_list_announcements_admin(org_id)
    return {"announcements": items}


@router.delete("/announcements/{announcement_id}", status_code=200)
async def delete_announcement(
    announcement_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Soft-delete an announcement (sets active=false)."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]
    deleted = await staff_comms_repo.db_soft_delete_announcement(org_id, announcement_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Anuncio no encontrado.")
    return {"success": True}


# ── Admin: Tasks ─────────────────────────────────────────────────────────────

@router.post("/tasks", status_code=201)
async def create_task(
    request: Request,
    body: TaskCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Create a new task for this org (admin only)."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]

    from app.routes.deps import get_current_user  # noqa: PLC0415
    user = await get_current_user(request)
    created_by = user.get("username")

    # Parse due_date string to a Python date object (or None).
    due_date = None
    if body.due_date:
        try:
            from datetime import date as _date  # noqa: PLC0415
            due_date = _date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="due_date must be YYYY-MM-DD")

    task = await staff_comms_repo.db_create_task(
        org_id=org_id,
        title=body.title,
        description=body.description,
        created_by=created_by,
        due_date=due_date,
    )
    return {"task": task}


@router.get("/tasks")
async def list_tasks_admin(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List active tasks with completions_count (admin view)."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]
    tasks = await staff_comms_repo.db_list_tasks_admin(org_id)
    return {"tasks": tasks}


@router.get("/tasks/{task_id}/completions")
async def get_task_completions(
    task_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return list of staff who completed this task (admin insight)."""
    _require_admin_role(restaurant)
    completions = await staff_comms_repo.db_list_completions_for_task(task_id)
    return {"completions": completions}


@router.delete("/tasks/{task_id}", status_code=200)
async def delete_task(
    task_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Soft-delete a task (sets active=false)."""
    _require_admin_role(restaurant)
    org_id = restaurant["id"]
    deleted = await staff_comms_repo.db_soft_delete_task(org_id, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")
    return {"success": True}


# ── Staff self-service: Announcements ────────────────────────────────────────

@router.get("/self/announcements")
async def self_announcements(
    user: dict = Depends(get_current_user_scoped),
):
    """Return currently active announcements for the authenticated staff member."""
    org_id = user.get("restaurant_id")
    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo determinar la organización.")

    now = datetime.now(tz=timezone.utc)
    items = await staff_comms_repo.db_list_announcements_active(int(org_id), now)
    return {"announcements": items}


# ── Staff self-service: Tasks ────────────────────────────────────────────────

@router.get("/self/tasks")
async def self_tasks(
    user: dict = Depends(get_current_user_scoped),
):
    """Return active tasks annotated with completed_by_me and completed_at."""
    org_id = user.get("restaurant_id")
    staff_id = None
    username: str = user.get("username", "")
    if username.startswith("staff:"):
        staff_id = username.split(":", 1)[1]

    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo determinar la organización.")

    tasks = await staff_comms_repo.db_list_tasks_active(int(org_id))

    # Single round-trip: get completions for all active tasks at once.
    if staff_id and tasks:
        task_ids = [t["id"] for t in tasks]
        completions_map = await staff_comms_repo.db_get_completions_for_staff(staff_id, task_ids)
    else:
        completions_map = {}

    annotated = []
    for t in tasks:
        comp = completions_map.get(t["id"])
        annotated.append({
            **t,
            "completed_by_me": comp is not None,
            "completed_at": comp["completed_at"] if comp else None,
        })

    return {"tasks": annotated}


@router.post("/self/tasks/{task_id}/complete", status_code=200)
async def self_complete_task(
    task_id: int,
    body: TaskCompleteBody,
    user: dict = Depends(get_current_user_scoped),
):
    """Mark a task as completed by the authenticated staff member."""
    org_id = user.get("restaurant_id")
    username: str = user.get("username", "")
    if not username.startswith("staff:"):
        raise HTTPException(status_code=403, detail="Solo el staff puede marcar tareas.")
    staff_id = username.split(":", 1)[1]

    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo determinar la organización.")

    try:
        completion = await staff_comms_repo.db_complete_task(
            task_id=task_id,
            staff_id=staff_id,
            org_id=int(org_id),
            notes=body.notes,
        )
    except Exception as exc:
        log.exception("staff_comms.complete_task_error", task_id=task_id, error=str(exc))
        raise HTTPException(status_code=404, detail="Tarea no encontrada o no pertenece a tu organización.")

    return {"completion": completion}


# ── Staff self-service: Tips ─────────────────────────────────────────────────

@router.get("/self/tips", status_code=200)
async def self_tips(
    user: dict = Depends(get_current_user_scoped),
):
    """Return this staff member's tip totals for today and this week.

    Reuses db_calculate_tips_by_attendance filtered to this staff's entries.
    The tip_distribution feature config must be set in features.tip_distribution
    for the org; if not configured, both totals return zero.

    Response shape:
      {
        "today":       float,   # COP amount accumulated today
        "today_count": int,     # number of table checks today
        "week":        float,   # COP amount accumulated this week (Mon–now)
        "week_count":  int,     # number of table checks this week
        "currency":    str      # always "COP" for now (TODO: multi-currency)
      }
    """
    org_id = user.get("restaurant_id")
    username: str = user.get("username", "")
    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo determinar la organización.")
    if not username.startswith("staff:"):
        raise HTTPException(status_code=403, detail="Solo el staff puede consultar sus propinas.")
    staff_id = username.split(":", 1)[1]

    now = datetime.now(tz=timezone.utc)

    # Period: today 00:00 UTC → now
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_str = today_start.isoformat()
    now_str = now.isoformat()

    # Period: Monday 00:00 UTC → now (day 0 = Monday in Python weekday())
    days_since_monday = now.weekday()
    monday_start = (now - timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    monday_start_str = monday_start.isoformat()

    today_data = await staff_repo.db_calculate_tips_for_staff(
        org_id=int(org_id),
        staff_id=staff_id,
        period_start=today_start_str,
        period_end=now_str,
    )
    week_data = await staff_repo.db_calculate_tips_for_staff(
        org_id=int(org_id),
        staff_id=staff_id,
        period_start=monday_start_str,
        period_end=now_str,
    )

    return {
        "today":       today_data["amount"],
        "today_count": today_data["count"],
        "week":        week_data["amount"],
        "week_count":  week_data["count"],
        "currency":    "COP",
    }


# ── Staff self-service: Upcoming shifts ──────────────────────────────────────

@router.get("/self/upcoming-shifts", status_code=200)
async def self_upcoming_shifts(
    user: dict = Depends(get_current_user_scoped),
    limit: int = Query(default=3, ge=1, le=10),
):
    """Return the next N calendar occurrences of this staff member's weekly schedule.

    Generates upcoming shift dates by projecting the weekly schedule pattern
    (staff_schedules rows) over the next 28 days.  The result is sorted by date
    ascending and capped at `limit` (default 3).

    Response shape:
      {
        "shifts": [
          {
            "date":           "2026-04-21",
            "day_of_week":    0,            # 0=Mon … 6=Sun
            "start_time":     "14:00",
            "end_time":       "22:00",
            "duration_hours": 8.0
          },
          ...
        ]
      }

    Timezone: defaults to America/Bogota (TODO: read from locations.timezone once
    multi-tz support is wired end-to-end — tracked as a known limitation).
    """
    username: str = user.get("username", "")
    if not username.startswith("staff:"):
        raise HTTPException(status_code=403, detail="Solo el staff puede consultar sus turnos.")
    staff_id = username.split(":", 1)[1]

    # TODO (multi-tz): resolve locations.timezone for this staff member's org
    # and pass it here instead of hardcoding.  Tracked in CLAUDE.md limitations.
    shifts = await staff_repo.db_get_staff_upcoming_shifts(
        staff_id=staff_id,
        limit=limit,
        timezone_name="America/Bogota",
    )
    return {"shifts": shifts}
