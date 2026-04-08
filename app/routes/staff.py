"""
Phase 6 — Staff, Shifts & Tips endpoints.

All routes are protected by:
  1. require_auth  (via get_current_restaurant)
  2. require_module('staff_tips')  — restaurant must have features.staff_tips = true

Layer rules:
  - HTTP parsing / validation only here.
  - Business logic lives in services/.
  - Raw SQL lives exclusively in database.py.
"""
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from app.services.money import to_decimal

from app.routes.deps import get_current_restaurant, require_module
from app.services import database as db

router = APIRouter(prefix="/api/staff", tags=["staff"])

# bcrypt context — 12 rounds is a good default for PIN hashing
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

_VALID_ROLES = {"mesero", "cocina", "bar", "caja", "gerente", "domiciliario", "otro"}

# All endpoints share these two dependencies:
#   • get_current_restaurant — resolves + returns the restaurant dict
#   • require_module         — raises 403 if staff_tips is not enabled

_MODULE_DEPS = [Depends(require_module("staff_tips"))]

# ── Pydantic models ──────────────────────────────────────────────────────────

class StaffCreate(BaseModel):
    name:            str       = Field(..., min_length=1, max_length=100)
    role:            str       = Field("mesero", min_length=1, max_length=50)
    roles:           list[str] = Field(default_factory=list)
    password:        str       = Field(..., min_length=4, max_length=100)
    phone:           str       = Field("", max_length=30)
    document_number: str       = Field("", max_length=50)


class StaffUpdate(BaseModel):
    name:            str | None       = Field(None, min_length=1, max_length=100)
    role:            str | None       = Field(None, min_length=1, max_length=50)
    roles:           list[str] | None = None
    password:        str | None       = Field(None, min_length=4, max_length=100)
    phone:           str | None       = Field(None, max_length=30)
    active:          bool | None      = None
    document_number: str | None       = Field(None, max_length=50)

class StaffPinLoginRequest(BaseModel):
    restaurant_id: int
    name: str = Field(..., min_length=1, max_length=100)
    pin:  str = Field(..., min_length=4, max_length=100)


def _staff_redirect(roles: list) -> str:
    """Return the best landing page URL for the given role set.
    Admins/managers go to /dashboard.
    All operational staff go to /staff-hq (personal HQ terminal).
    """
    admin_roles = {"owner", "admin", "gerente"}
    if any(r in admin_roles for r in roles):
        return "/dashboard"
    return "/staff-hq"


class ClockInRequest(BaseModel):
    staff_id: str = Field(..., description="UUID of the staff member")


class ClockOutRequest(BaseModel):
    staff_id: str = Field(..., description="UUID of the staff member")


class ShiftsQuery(BaseModel):
    date_from: str = Field(..., description="ISO datetime start (inclusive)")
    date_to:   str = Field(..., description="ISO datetime end (exclusive)")


class TipCutRequest(BaseModel):
    period_start: str   = Field(..., description="ISO datetime start")
    period_end:   str   = Field(..., description="ISO datetime end")
    total_tips:   float = Field(..., ge=0, description="Total tip amount to distribute")


# ── Staff roster ─────────────────────────────────────────────────────────────

@router.get("", dependencies=_MODULE_DEPS)
async def list_staff(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Retorna el staff filtrado por la sucursal seleccionada en el selector global."""
    # 🛡️ FILTRO GLOBAL: Si el Owner seleccionó una sucursal, usamos ese ID
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    
    if branch_header and branch_header.isdigit():
        # Como get_current_restaurant ya validó el acceso, 
        # podemos confiar en el ID del header si el usuario es Owner/Admin
        branch_id = int(branch_header)

    staff = await db.db_get_staff(branch_id)
    return {"staff": staff}


@router.post("", dependencies=_MODULE_DEPS, status_code=201)
async def create_staff(
    request: Request,
    body: StaffCreate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Crea un empleado en la sucursal que el Owner tenga seleccionada."""
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        branch_id = int(branch_header)

    pin_hash = _pwd_ctx.hash(body.password)
    roles = [r.strip().lower() for r in body.roles if r.strip()] if body.roles else [body.role.strip().lower()]
    
    member = await db.db_create_staff(
        restaurant_id=branch_id,
        name=body.name,
        role=roles[0] if roles else "mesero",
        pin_hash=pin_hash,
        phone=body.phone,
        roles=roles or ["mesero"],
        document_number=body.document_number,
    )
    return {"staff": member}
    
@router.post("/pin-login", status_code=200)
async def staff_pin_login(body: StaffPinLoginRequest):
    member = await db.db_get_staff_for_pin_login(body.restaurant_id, body.name)
    if not member:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    if not _pwd_ctx.verify(body.pin, member["pin"]):
        raise HTTPException(status_code=401, detail="PIN incorrecto.")

    token = secrets.token_hex(32)
    await db.db_save_session(token, f"staff:{member['id']}")

    roles = member.get("roles") or [member.get("role", "mesero")]

    # ✅ FIX: traer datos del restaurante para guardar en localStorage
    restaurant_data = await db.db_get_restaurant_by_id(body.restaurant_id)
    raw_features = restaurant_data.get("features") or {} if restaurant_data else {}
    if isinstance(raw_features, str):
        import json as _j
        try: raw_features = _j.loads(raw_features)
        except: raw_features = {}

    return {
        "token":        token,
        "access_token": token,   # alias for reloj.html WebAuthn registration flow
        "staff_id": member["id"],
        "roles":    roles,
        "name":     member["name"],
        "redirect": _staff_redirect(roles),
        "restaurant": {
            "name":             restaurant_data.get("name", "") if restaurant_data else "",
            "whatsapp_number":  restaurant_data.get("whatsapp_number", "") if restaurant_data else "",
            "locale":           raw_features.get("locale", "es-CO"),
            "currency":         raw_features.get("currency", "COP"),
            "features":         raw_features,
        }
    }

@router.put("/{staff_id}", dependencies=_MODULE_DEPS)
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Update mutable staff fields. PIN is re-hashed if provided."""
    patch = body.model_dump(exclude_none=True)

    if "password" in patch:
        patch["pin"] = _pwd_ctx.hash(patch.pop("password"))

    if "roles" in patch:
        patch["roles"] = [r.strip().lower() for r in patch["roles"] if r.strip()]
        if patch["roles"] and "role" not in patch:
            patch["role"] = patch["roles"][0]

    if not patch:
        raise HTTPException(status_code=422, detail="No fields to update.")

    updated = await db.db_update_staff(staff_id, restaurant["id"], patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    return {"staff": updated}


@router.delete("/{staff_id}", dependencies=_MODULE_DEPS, status_code=200)
async def delete_staff(
    staff_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Elimina permanentemente un empleado del roster."""
    deleted = await db.db_delete_staff(staff_id, restaurant["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    return {"success": True}


# ── Self clock-in / clock-out (para operativos autenticados via token) ────────
# No necesita get_current_restaurant — resuelve restaurant_id desde la tabla staff.

@router.post("/self/clock-in", status_code=200)
async def self_clock_in(request: Request):
    """El operativo registra su propia entrada usando su Bearer token."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await db.db_get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT restaurant_id FROM staff WHERE id=$1::uuid", staff_id)
    if not row:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    try:
        shift = await db.db_clock_in(staff_id, row["restaurant_id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"shift": shift}


@router.post("/self/clock-out", status_code=200)
async def self_clock_out(request: Request):
    """El operativo registra su propia salida usando su Bearer token."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await db.db_get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT restaurant_id FROM staff WHERE id=$1::uuid", staff_id)
    if not row:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    shift = await db.db_clock_out(staff_id, row["restaurant_id"])
    if not shift:
        raise HTTPException(status_code=404, detail="No hay turno abierto para este empleado.")
    return {"shift": shift}


# ── Clock-in / Clock-out (admin/dashboard — requiere get_current_restaurant) ──

@router.post("/clock-in", dependencies=_MODULE_DEPS)
async def clock_in(
    body: ClockInRequest,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Open a new shift for the given staff_id.
    Returns 409 if the employee already has an open shift
    (enforced by the partial unique index uq_staff_shifts_one_open).
    """
    try:
        shift = await db.db_clock_in(body.staff_id, restaurant["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"shift": shift}


@router.post("/clock-out", dependencies=_MODULE_DEPS)
async def clock_out(
    body: ClockOutRequest,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Close the open shift for the given staff_id.
    Returns 404 if no open shift was found.
    """
    shift = await db.db_clock_out(body.staff_id, restaurant["id"])
    if not shift:
        raise HTTPException(status_code=404, detail="No hay turno abierto para este empleado.")
    return {"shift": shift}


@router.get("/open-shifts", dependencies=_MODULE_DEPS)
async def open_shifts(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return all currently open shifts for the restaurant."""
    shifts = await db.db_get_open_shifts(restaurant["id"])
    return {"shifts": shifts}


@router.get("/shifts", dependencies=_MODULE_DEPS)
async def get_shifts(
    date_from: str,
    date_to: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Return shift history for [date_from, date_to).
    date_from / date_to: ISO 8601 strings, e.g. '2026-03-01T00:00:00Z'.
    """
    shifts = await db.db_get_shifts(restaurant["id"], date_from, date_to)
    return {"shifts": shifts}


# ── Tip distributions ────────────────────────────────────────────────────────

@router.get("/tip-distributions", dependencies=_MODULE_DEPS)
async def tip_distributions(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return the 20 most recent tip distribution cuts."""
    cuts = await db.db_get_tip_distributions(restaurant["id"])
    return {"distributions": cuts}


class TipsAutoRequest(BaseModel):
    period_start: str
    period_end: str
    branch_id: int | None = None


@router.get("/tips/auto", dependencies=_MODULE_DEPS)
async def tips_auto(
    period_start: str,
    period_end: str,
    branch_id: int | None = None,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return auto-calculated tip distribution based on attendance overlap."""
    result = await db.db_calculate_tips_by_attendance(
        restaurant_id=restaurant["id"],
        period_start=period_start,
        period_end=period_end,
        branch_id=branch_id,
    )
    return result


class TipDistributionConfig(BaseModel):
    config: dict[str, float]


@router.patch("/tip-distribution", dependencies=_MODULE_DEPS, status_code=200)
async def update_tip_distribution(
    body: TipDistributionConfig,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Update the tip distribution % config for all roles."""
    total = sum(body.config.values())
    if abs(total - 100.0) > 0.01 and total > 0:
        if total > 100.0:
            raise HTTPException(status_code=400, detail=f"Los porcentajes suman {total:.1f}%, no pueden superar 100%")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE restaurants
               SET features = jsonb_set(COALESCE(features, '{}'), '{tip_distribution}', $1::jsonb)
               WHERE id = $2""",
            json.dumps(body.config), restaurant["id"],
        )
    return {"success": True, "config": body.config}


# ── Break management (self-service) ─────────────────────────────────────────

class BreakRequest(BaseModel):
    """No body needed - uses authenticated staff_id."""
    pass


@router.post("/self/break-start", status_code=200)
async def self_break_start(request: Request):
    """Start a break. Staff must have an open shift."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await db.db_get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key.split(":", 1)[1]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT restaurant_id FROM staff WHERE id=$1::uuid", staff_id)
    if not row:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    shifts = await db.db_get_open_shifts(row["restaurant_id"])
    open_shift = next((s for s in shifts if str(s["staff_id"]) == staff_id), None)
    if not open_shift:
        raise HTTPException(status_code=404, detail="No tienes un turno abierto.")
    try:
        brk = await db.db_start_break(staff_id, open_shift["id"])
        return {"break": brk}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/self/break-end", status_code=200)
async def self_break_end(request: Request):
    """End current break."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await db.db_get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key.split(":", 1)[1]
    brk = await db.db_end_break(staff_id)
    if not brk:
        raise HTTPException(status_code=404, detail="No tienes un break abierto.")
    return {"break": brk}


# ── Self-service endpoints para Staff HQ ─────────────────────────────────────

async def _resolve_staff_from_token(request: Request) -> dict:
    """Helper: extrae staff_id desde Bearer token y retorna su fila completa."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await db.db_get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, "
            "document_number, hourly_rate, photo_url FROM staff WHERE id=$1::uuid",
            staff_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    d = dict(row)
    d["id"] = str(d["id"])
    roles_list = d.get("roles") or []
    if not roles_list and d.get("role"):
        roles_list = [d["role"]]
    d["roles"] = roles_list
    return d


@router.get("/self/profile", status_code=200)
async def self_profile(request: Request):
    """Retorna el perfil completo del operativo autenticado, incluyendo estado de turno y break."""
    member = await _resolve_staff_from_token(request)
    staff_id = member["id"]
    restaurant_id = member["restaurant_id"]

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        # Turno abierto
        shift_row = await conn.fetchrow(
            "SELECT id::text, clock_in FROM staff_shifts "
            "WHERE staff_id=$1::uuid AND clock_out IS NULL LIMIT 1",
            staff_id,
        )
        # Break abierto
        break_row = await conn.fetchrow(
            "SELECT id::text, break_start FROM staff_breaks "
            "WHERE staff_id=$1::uuid AND break_end IS NULL LIMIT 1",
            staff_id,
        )

    return {
        "id":              member["id"],
        "name":            member["name"],
        "roles":           member["roles"],
        "role":            member["role"],
        "phone":           member["phone"],
        "document_number": member["document_number"],
        "hourly_rate":     float(to_decimal(member["hourly_rate"] or 0)),  # JSON boundary
        "photo_url":       member["photo_url"],
        "restaurant_id":   restaurant_id,
        "current_shift":   db._serialize(dict(shift_row)) if shift_row else None,
        "current_break":   db._serialize(dict(break_row)) if break_row else None,
    }


@router.get("/self/timecard", status_code=200)
async def self_timecard(request: Request, week_start: str = None, week_end: str = None):
    """Retorna el timecard semanal personal del operativo autenticado."""
    member = await _resolve_staff_from_token(request)
    staff_id = member["id"]
    restaurant_id = member["restaurant_id"]

    from datetime import date, timedelta
    today = date.today()
    if not week_start:
        monday = today - timedelta(days=today.weekday())
        week_start = monday.isoformat()
    if not week_end:
        week_end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()

    # asyncpg requires date objects (not strings) for DATE parameter binding
    ws_date = date.fromisoformat(week_start)
    we_date = date.fromisoformat(week_end)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                s.id::text      AS shift_id,
                s.clock_in,
                s.clock_out,
                s.clock_in::date AS work_date,
                COALESCE(
                    EXTRACT(EPOCH FROM (COALESCE(s.clock_out, NOW()) - s.clock_in)) / 3600,
                    0
                )::numeric(8,2) AS gross_hours,
                COALESCE((
                    SELECT SUM(EXTRACT(EPOCH FROM (COALESCE(b.break_end, NOW()) - b.break_start)) / 3600)
                    FROM staff_breaks b
                    WHERE b.shift_id = s.id
                ), 0)::numeric(8,2) AS break_hours
            FROM staff_shifts s
            WHERE s.staff_id = $1::uuid
              AND s.clock_in::date BETWEEN $2 AND $3
            ORDER BY s.clock_in ASC
            """,
            staff_id, ws_date, we_date,
        )

    # Fetch schedules for the week
    async with pool.acquire() as conn:
        sched_rows = await conn.fetch(
            "SELECT day_of_week, start_time, end_time FROM staff_schedules "
            "WHERE staff_id=$1::uuid AND active=true",
            staff_id,
        )

    sched_map = {r["day_of_week"]: {"start": str(r["start_time"]), "end": str(r["end_time"])} for r in sched_rows}

    # Fetch attendance deductions for the period
    async with pool.acquire() as conn:
        ded_rows = await conn.fetch(
            "SELECT shift_id::text, type, minutes_diff, deduction_amount "
            "FROM attendance_deductions "
            "WHERE staff_id=$1::uuid AND created_at::date BETWEEN $2 AND $3",
            staff_id, ws_date, we_date,
        )

    ded_map: dict = {}
    for d in ded_rows:
        sid = str(d["shift_id"])
        ded_map.setdefault(sid, []).append(dict(d))

    entries = []
    for r in rows:
        gross = float(r["gross_hours"] or 0)
        brk   = float(r["break_hours"] or 0)
        net   = round(max(gross - brk, 0), 2)
        dow   = r["work_date"].weekday()
        sched = sched_map.get(dow)
        shift_id = r["shift_id"]
        deductions = ded_map.get(shift_id, [])
        is_late = any(d["type"] == "tardiness" for d in deductions)
        is_early = any(d["type"] == "early_departure" for d in deductions)
        entries.append({
            "shift_id":    shift_id,
            "work_date":   r["work_date"].isoformat(),
            "clock_in":    r["clock_in"].isoformat() if r["clock_in"] else None,
            "clock_out":   r["clock_out"].isoformat() if r["clock_out"] else None,
            "gross_hours": gross,
            "break_hours": brk,
            "net_hours":   net,
            "schedule":    sched,
            "is_late":     is_late,
            "is_early_departure": is_early,
            "deductions":  deductions,
        })

    total_net = round(sum(e["net_hours"] for e in entries), 2)
    return {
        "week_start":  week_start,
        "week_end":    week_end,
        "staff_id":    staff_id,
        "staff_name":  member["name"],
        "entries":     entries,
        "total_hours": total_net,
    }


@router.get("/self/schedule", status_code=200)
async def self_schedule(request: Request):
    """Retorna el horario semanal del operativo autenticado."""
    member = await _resolve_staff_from_token(request)
    staff_id = member["id"]
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, day_of_week, start_time, end_time, active "
            "FROM staff_schedules WHERE staff_id=$1::uuid AND active=true "
            "ORDER BY day_of_week ASC",
            staff_id,
        )
    days = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    return {
        "schedule": [
            {
                "id":          r["id"],
                "day_of_week": r["day_of_week"],
                "day_name":    days[r["day_of_week"]],
                "start_time":  str(r["start_time"]),
                "end_time":    str(r["end_time"]),
            }
            for r in rows
        ]
    }


# ── Shift edit (admin) ───────────────────────────────────────────────────────

class ShiftEditBody(BaseModel):
    clock_in:  str | None = None   # ISO datetime string
    clock_out: str | None = None   # ISO datetime string
    notes:     str | None = None


@router.post("/shifts/{shift_id}/edit", dependencies=_MODULE_DEPS)
async def edit_shift(
    request: Request,
    shift_id: str,
    body: ShiftEditBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Admin: correct shift times."""
    result = await db.db_edit_shift(
        shift_id, restaurant["id"],
        clock_in=body.clock_in, clock_out=body.clock_out, notes=body.notes,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Turno no encontrado.")
    return {"shift": result}


# ── Schedule management (admin) ──────────────────────────────────────────────

class ScheduleBody(BaseModel):
    staff_id:    str
    day_of_week: int = Field(..., ge=0, le=6)  # 0=Monday
    start_time:  str  # "HH:MM" format
    end_time:    str  # "HH:MM" format


class ScheduleBulkBody(BaseModel):
    entries: list[ScheduleBody]


@router.post("/schedules/bulk", dependencies=_MODULE_DEPS, status_code=200)
async def save_schedules_bulk(
    body: ScheduleBulkBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Bulk create/update schedules for multiple staff members."""
    from datetime import time
    entries = []
    for e in body.entries:
        entries.append({
            "staff_id": e.staff_id,
            "day_of_week": e.day_of_week,
            "start_time": time.fromisoformat(e.start_time),
            "end_time": time.fromisoformat(e.end_time),
        })
    results = await db.db_bulk_upsert_schedules(entries, restaurant["id"])
    return {"schedules": results}


@router.post("/schedules", dependencies=_MODULE_DEPS, status_code=200)
async def save_schedule(
    body: ScheduleBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Create or update schedule for staff member on a specific day."""
    from datetime import time
    start = time.fromisoformat(body.start_time)
    end = time.fromisoformat(body.end_time)
    result = await db.db_upsert_schedule(
        body.staff_id, restaurant["id"], body.day_of_week, start, end,
    )
    return {"schedule": result}


@router.get("/schedules", dependencies=_MODULE_DEPS)
async def list_schedules(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Get all schedules for the restaurant."""
    schedules = await db.db_get_schedules(restaurant["id"])
    return {"schedules": schedules}


@router.delete("/schedules/{schedule_id}", dependencies=_MODULE_DEPS, status_code=200)
async def delete_schedule(
    schedule_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Delete a schedule entry by ID."""
    deleted = await db.db_delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Horario no encontrado.")
    return {"success": True}


# ── Timecard (admin) ─────────────────────────────────────────────────────────

@router.get("/timecard", dependencies=_MODULE_DEPS)
async def get_timecard(
    week_start: str,
    week_end: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Weekly timecard: hours per employee per day."""
    data = await db.db_get_timecard(restaurant["id"], week_start, week_end)
    return {"timecard": data}


# ── Overtime report (admin) ──────────────────────────────────────────────────

@router.get("/overtime", dependencies=_MODULE_DEPS)
async def get_overtime(
    date_from: str,
    date_to: str,
    restaurant: dict = Depends(get_current_restaurant),
    daily_threshold: float = 8.0,
    weekly_threshold: float = 40.0,
):
    """Overtime report for a date range."""
    data = await db.db_get_overtime_report(
        restaurant["id"], date_from, date_to, daily_threshold, weekly_threshold,
    )
    return {"overtime": data}


# ── Attendance report (admin) ────────────────────────────────────────────────

@router.get("/attendance", dependencies=_MODULE_DEPS)
async def get_attendance(
    date_from: str,
    date_to: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Compare actual clock-in with scheduled times."""
    data = await db.db_get_attendance_report(restaurant["id"], date_from, date_to)
    return {"attendance": data}


class DeductionItemCreate(BaseModel):
    category: str = Field("custom", max_length=50)
    label:    str = Field(..., min_length=1, max_length=100)
    type:     str = Field("fixed", pattern="^(fixed|percentage)$")
    amount:   float = Field(..., ge=0)


class DeductionItemUpdate(BaseModel):
    category: str   | None = None
    label:    str   | None = Field(None, min_length=1, max_length=100)
    type:     str   | None = None
    amount:   float | None = Field(None, ge=0)
    active:   bool  | None = None


# ── Deduction items CRUD (admin) ─────────────────────────────────────────────

@router.get("/{staff_id}/deductions", dependencies=_MODULE_DEPS)
async def list_deduction_items(
    staff_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """List all deduction items for a staff member."""
    items = await db.db_list_deduction_items(staff_id, restaurant["id"])
    return {"items": items}


@router.post("/{staff_id}/deductions", dependencies=_MODULE_DEPS, status_code=201)
async def create_deduction_item(
    staff_id: str,
    body: DeductionItemCreate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Create a manual deduction item for a staff member."""
    item = await db.db_create_deduction_item(
        staff_id=staff_id,
        restaurant_id=restaurant["id"],
        category=body.category,
        label=body.label,
        item_type=body.type,
        amount=body.amount,
    )
    return {"item": item}


@router.patch("/deductions/{item_id}", dependencies=_MODULE_DEPS)
async def update_deduction_item(
    item_id: str,
    body: DeductionItemUpdate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Edit or deactivate a deduction item."""
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="No fields to update.")
    updated = await db.db_update_deduction_item(item_id, restaurant["id"], patch)
    if not updated:
        raise HTTPException(status_code=404, detail="Item no encontrado.")
    return {"item": updated}


@router.delete("/deductions/{item_id}", dependencies=_MODULE_DEPS, status_code=200)
async def delete_deduction_item(
    item_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Delete a deduction item."""
    deleted = await db.db_delete_deduction_item(item_id, restaurant["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Item no encontrado.")
    return {"success": True}


# ── Payroll endpoints ─────────────────────────────────────────────────────────

@router.get("/payroll/calculate", dependencies=_MODULE_DEPS)
async def payroll_calculate(
    request: Request,
    period_start: str,
    period_end: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Calculate payroll for all staff in the selected branch/period."""
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        branch_id = int(branch_header)
    entries = await db.db_calculate_payroll(branch_id, period_start, period_end)
    return {"entries": entries}


@router.post("/payroll/runs", dependencies=_MODULE_DEPS, status_code=201)
async def save_payroll_run(
    request: Request,
    body: dict,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Save payroll calculation as a draft run."""
    period_start = body.get("period_start")
    period_end   = body.get("period_end")
    if not period_start or not period_end:
        raise HTTPException(status_code=422, detail="period_start y period_end son requeridos.")
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        branch_id = int(branch_header)
    entries = await db.db_calculate_payroll(branch_id, period_start, period_end)
    run = await db.db_save_payroll_run(
        restaurant_id=branch_id,
        period_start=period_start,
        period_end=period_end,
        snapshot=entries,
        created_by=restaurant.get("whatsapp_number", ""),
    )
    return {"run": run}


@router.get("/payroll/runs", dependencies=_MODULE_DEPS)
async def list_payroll_runs(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant),
):
    """List recent payroll runs."""
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        branch_id = int(branch_header)
    runs = await db.db_get_payroll_runs(branch_id)
    return {"runs": runs}


# ── Contract templates (admin/owner) ─────────────────────────────────────────

class ContractTemplateCreate(BaseModel):
    name:               str     = Field(..., min_length=1, max_length=100)
    weekly_hours:       float   = Field(44.0, ge=1, le=84)
    monthly_salary:     Decimal = Field(Decimal("0"), ge=0)
    pay_period:         str     = Field("biweekly", pattern="^(monthly|biweekly|weekly)$")
    transport_subsidy:  Decimal = Field(Decimal("0"), ge=0)
    arl_pct:            Decimal = Field(Decimal("0.00522"), ge=0, le=1)
    health_pct:         Decimal = Field(Decimal("0.04"), ge=0, le=1)
    pension_pct:        Decimal = Field(Decimal("0.04"), ge=0, le=1)
    other_benefits:     dict    = Field(default_factory=dict)
    breaks_billable:    bool    = True
    lunch_billable:     bool    = False
    lunch_minutes:      int     = Field(60, ge=0, le=120)


class ContractTemplateUpdate(BaseModel):
    name:               str     | None = Field(None, min_length=1, max_length=100)
    weekly_hours:       float   | None = Field(None, ge=1, le=84)
    monthly_salary:     Decimal | None = Field(None, ge=0)
    pay_period:         str     | None = Field(None, pattern="^(monthly|biweekly|weekly)$")
    transport_subsidy:  Decimal | None = Field(None, ge=0)
    arl_pct:            Decimal | None = Field(None, ge=0, le=1)
    health_pct:         Decimal | None = Field(None, ge=0, le=1)
    pension_pct:        Decimal | None = Field(None, ge=0, le=1)
    other_benefits:     dict    | None = None
    breaks_billable:    bool    | None = None
    lunch_billable:     bool    | None = None
    lunch_minutes:      int   | None = Field(None, ge=0, le=120)
    active:             bool  | None = None


class StaffContractAssign(BaseModel):
    template_id:    str | None = None
    overrides:      dict       = Field(default_factory=dict)
    contract_start: str | None = None


@router.get("/payroll/contracts", dependencies=_MODULE_DEPS)
async def list_contract_templates(
    restaurant: dict = Depends(get_current_restaurant),
):
    """List all contract templates for the restaurant."""
    templates = await db.db_list_contract_templates(restaurant["id"])
    return {"templates": templates}


@router.post("/payroll/contracts", dependencies=_MODULE_DEPS, status_code=201)
async def create_contract_template(
    body: ContractTemplateCreate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Create a new contract template."""
    template = await db.db_create_contract_template(restaurant["id"], body.model_dump())
    return {"template": template}


@router.patch("/payroll/contracts/{template_id}", dependencies=_MODULE_DEPS)
async def update_contract_template(
    template_id: str,
    body: ContractTemplateUpdate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Update a contract template."""
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    template = await db.db_update_contract_template(template_id, restaurant["id"], data)
    if not template:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada.")
    return {"template": template}


@router.delete("/payroll/contracts/{template_id}", dependencies=_MODULE_DEPS)
async def delete_contract_template(
    template_id: str,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Delete a contract template (fails if staff are assigned to it)."""
    deleted = await db.db_delete_contract_template(template_id, restaurant["id"])
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="No se puede eliminar: hay empleados asignados a esta plantilla.",
        )
    return {"success": True}


@router.patch("/{staff_id}/contract", dependencies=_MODULE_DEPS)
async def assign_staff_contract(
    staff_id: str,
    body: StaffContractAssign,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Assign or clear a contract template for a staff member."""
    result = await db.db_assign_staff_contract(
        staff_id=staff_id,
        restaurant_id=restaurant["id"],
        template_id=body.template_id,
        overrides=body.overrides,
        contract_start=body.contract_start,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    return {"staff": result}


# ── Overtime approval (admin/owner) ──────────────────────────────────────────

class OvertimeReview(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    notes:  str = Field("", max_length=500)


@router.get("/payroll/overtime", dependencies=_MODULE_DEPS)
async def list_overtime_requests(
    request: Request,
    week_start: str | None = None,
    status: str | None = None,
    restaurant: dict = Depends(get_current_restaurant),
):
    """List overtime requests for review."""
    branch_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit():
        branch_id = int(branch_header)
    requests = await db.db_list_overtime_requests(branch_id, week_start, status)
    return {"overtime_requests": requests}


@router.patch("/payroll/overtime/{request_id}", dependencies=_MODULE_DEPS)
async def review_overtime_request(
    request_id: str,
    body: OvertimeReview,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Approve or reject an overtime request."""
    result = await db.db_review_overtime_request(
        request_id=request_id,
        restaurant_id=restaurant["id"],
        status=body.status,
        approved_by=None,  # Could pass restaurant admin ID if available
        notes=body.notes,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Solicitud de overtime no encontrada.")
    return {"overtime_request": result}
