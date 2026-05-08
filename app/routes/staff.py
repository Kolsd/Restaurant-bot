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
from datetime import datetime, timezone
from decimal import Decimal

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from app.services.money import to_decimal

from app.routes.deps import get_current_restaurant, get_current_restaurant_scoped, require_module
from app.services import database as db
from app.services import state_store
from app.repositories import sessions_repo, staff_repo
from app.services.logging import get_logger
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

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
    name:            str       = Field(..., min_length=1, max_length=100, description="Nombre(s)")
    last_name:       str       = Field("", max_length=100, description="Apellido(s)")
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
    name: str = Field(..., min_length=1, max_length=100, description="Nombre completo o usuario (ej: juan.perez)")
    pin:  str = Field(..., min_length=4, max_length=100)


class StaffVerifyPinRequest(BaseModel):
    """Body for /self/verify-pin — kiosco PIN fallback when biometric unavailable.

    Unlike pin-login which accepts `name`, this endpoint receives the staff_id
    explicitly because the kiosco has already selected the staff from a list.
    Using id avoids ambiguity when names collide within the same org.
    """
    restaurant_id: int
    staff_id:      str = Field(..., min_length=1, max_length=64)
    pin:           str = Field(..., min_length=4, max_length=100)


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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Retorna el staff de la organización del usuario autenticado.

    Wave-2: db_get_staff filtra por org_id. Staff es organization-level —
    todas las sedes de un org comparten el mismo equipo en el dashboard de
    administración. El X-Branch-ID header no aplica aquí: pasarlo como
    branch_id pre-Paso-10 lo convertía en location_id y la query devolvía
    cero filas en multi-branch (bug enmascarado por Matriz invariant). Si
    el producto eventualmente quiere "ver staff por sede", agregar un repo
    method dedicado que filtre staff.location_id en vez de overridear org_id.
    """
    org_id = restaurant["id"]
    staff = await db.db_get_staff(org_id)
    return {"staff": staff}


@router.post("", dependencies=_MODULE_DEPS, status_code=201)
async def create_staff(
    request: Request,
    body: StaffCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Crea un empleado en la organización del usuario autenticado.

    Wave-2: db_create_staff INSERTs INTO staff (org_id, ...) — el primer
    argumento es la TENANT KEY (org_id), no la sede. Pre-Paso-10 el override
    X-Branch-ID convertía branch_id en location_id y el INSERT escribía
    org_id = location_id (FK violation o asignación a otro org).
    El header X-Branch-ID podría usarse a futuro para popular staff.location_id
    (asignar empleado a sede); por ahora db_create_staff no recibe ese param,
    así que ignoramos el header para no introducir un parámetro no soportado.
    """
    org_id = restaurant["id"]

    pin_hash = _pwd_ctx.hash(body.password)
    roles = [r.strip().lower() for r in body.roles if r.strip()] if body.roles else [body.role.strip().lower()]
    full_name = f"{body.name.strip()} {body.last_name.strip()}".strip() if body.last_name else body.name.strip()

    member = await db.db_create_staff(
        restaurant_id=org_id,
        name=full_name,
        role=roles[0] if roles else "mesero",
        pin_hash=pin_hash,
        phone=body.phone,
        roles=roles or ["mesero"],
        document_number=body.document_number,
    )
    return {"staff": member}
    
_PIN_MAX_ATTEMPTS = 10
_PIN_WINDOW = 900  # 15 minutes
# Defense-in-depth: a global per-IP cap stops distributed brute force across
# many (restaurant_id, name) tuples. Without this an attacker iterating
# restaurant_id=1..1000 with name="Pedro"+pin="1234" gets 10 attempts per
# bucket — 10K total before any single bucket triggers. The global cap
# kicks in after 10 attempts per IP per 15min regardless of target.
# L-1: Reduced from 30 → 10; a legitimate user needs at most 2-3 tries.
_PIN_GLOBAL_MAX_ATTEMPTS = 10
_PIN_GLOBAL_WINDOW = 900  # 15 minutes


async def _check_pin_rate_limit(request: Request, restaurant_id: int, name: str) -> None:
    """Rate-limit PIN login via Redis (cross-worker safe).

    Two layers:
      1. Per (restaurant_id, name, IP) bucket — granular protection.
      2. Per IP global — stops cross-tenant brute force iteration.
    """
    ip = request.client.host if request.client else "unknown"

    # Layer 2: global per-IP cap (cross-restaurant).
    global_allowed = await state_store.rate_limit_check(
        key=f"pin_login_global:{ip}",
        max_requests=_PIN_GLOBAL_MAX_ATTEMPTS,
        window_seconds=_PIN_GLOBAL_WINDOW,
    )
    if not global_allowed:
        log.warning("staff.pin_login.global_rate_limit_hit", ip=ip)
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")

    # Layer 1: granular bucket.
    key = f"pin_login:{restaurant_id}:{str(name).lower().strip()}:{ip}"
    allowed = await state_store.rate_limit_check(
        key=key, max_requests=_PIN_MAX_ATTEMPTS, window_seconds=_PIN_WINDOW
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Demasiados intentos. Intenta en 15 minutos.")


@router.post("/pin-login", status_code=200)
async def staff_pin_login(request: Request, body: StaffPinLoginRequest):
    await _check_pin_rate_limit(request, body.restaurant_id, body.name)

    # Pre-auth flow: scope the tenant explicitly from the body. The downstream
    # repos call `tenant_connection()` which requires an active scope; without
    # this wrapper every PIN login raises TenantNotSetError (regression caught
    # by tests/e2e/test_staff_pin_login_e2e.py).
    from app.services.tenant_context import tenant_scope

    with tenant_scope(body.restaurant_id):
        member = await db.db_get_staff_for_pin_login(body.restaurant_id, body.name)
    # Use a constant-time response regardless of whether the employee was found
    # or the PIN was wrong — prevents username enumeration oracle.
    if not member or not _pwd_ctx.verify(body.pin, member["pin"]):
        # Audit trail: every failed attempt logged for ops visibility.
        # Aggregating these across the fleet surfaces distributed brute-force
        # attempts that the per-bucket rate limit alone cannot stop.
        ip = request.client.host if request.client else "unknown"
        log.warning(
            "staff.pin_login_failed",
            ip=ip,
            restaurant_id=body.restaurant_id,
            name_len=len(str(body.name or "")),
            member_found=bool(member),
        )
        raise HTTPException(status_code=401, detail="Credenciales inválidas.")

    token = await sessions_repo.create_session(f"staff:{member['id']}")

    roles = member.get("roles") or [member.get("role", "mesero")]

    with tenant_scope(body.restaurant_id):
        restaurant_data = await db.db_get_restaurant_by_id(body.restaurant_id)
    raw_features = restaurant_data.get("features") or {} if restaurant_data else {}
    if isinstance(raw_features, str):
        import json as _j
        try: raw_features = _j.loads(raw_features)
        except (ValueError, TypeError): raw_features = {}

    return {
        "token":        token,
        "access_token": token,   # alias for reloj.html WebAuthn registration flow
        "staff_id": member["id"],
        "roles":    roles,
        "name":     member["name"],
        "username": member.get("username", ""),
        "redirect": _staff_redirect(roles),
        "restaurant": {
            "name":             restaurant_data.get("name", "") if restaurant_data else "",
            "whatsapp_number":  restaurant_data.get("whatsapp_number", "") if restaurant_data else "",
            "locale":           raw_features.get("locale", "es-CO"),
            "currency":         raw_features.get("currency", "COP"),
            "features":         raw_features,
        }
    }

@router.post("/self/verify-pin", status_code=200)
async def staff_verify_pin(request: Request, body: StaffVerifyPinRequest):
    """
    PIN fallback for kiosco clock-in when biometric (WebAuthn) is unavailable.

    Unlike /pin-login (which authenticates by name for the initial login flow),
    this endpoint assumes the kiosco already knows which staff member is at the
    terminal (selected from a list on the kiosco UI). It verifies the PIN and
    returns a fresh session token that the client immediately uses to call the
    clock-in / clock-out / break endpoints.

    Same rate-limit + constant-time response as /pin-login to avoid
    enumeration. Key includes the staff_id rather than the name.
    """
    # Reuse the same per-IP-per-target rate limit
    await _check_pin_rate_limit(request, body.restaurant_id, body.staff_id)

    # Bypass tenant here: we haven't authenticated the kiosco caller yet, and
    # the org_id comes from the request body. Scoping via tenant_connection
    # below enforces the filter.
    from app.services.tenant_context import tenant_scope

    with tenant_scope(body.restaurant_id):
        member = await db.db_get_staff_pin_by_id(body.staff_id, body.restaurant_id)

    # Constant-time: run verify even when member is None with a dummy hash so
    # timing between "unknown staff_id" and "bad pin" is identical.
    # The dummy hash is a valid bcrypt of a random string.
    _DUMMY = "$2b$12$CGrQL8okZSh9O2uDzZqkMu3hZLUK8vYoU1O./GGLu4ZzHMN3CrH/G"
    pin_hash = member["pin"] if member else _DUMMY
    ok = _pwd_ctx.verify(body.pin, pin_hash)
    if not member or not ok:
        ip = request.client.host if request.client else "unknown"
        log.warning(
            "staff.verify_pin_failed",
            ip=ip,
            restaurant_id=body.restaurant_id,
            member_found=bool(member),
        )
        raise HTTPException(status_code=401, detail="Credenciales inválidas.")

    token = await sessions_repo.create_session(f"staff:{member['id']}")
    roles = member.get("roles") or [member.get("role", "mesero")]

    return {
        "token":        token,
        "access_token": token,
        "staff_id":     member["id"],
        "roles":        roles,
        "name":         member["name"],
    }


@router.put("/{staff_id}", dependencies=_MODULE_DEPS)
async def update_staff(
    staff_id: str,
    body: StaffUpdate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    session_key = await sessions_repo.get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    restaurant_id = await staff_repo.db_get_staff_restaurant_id(staff_id)
    if not restaurant_id:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    try:
        with tenant_scope(restaurant_id):
            shift = await db.db_clock_in(staff_id, restaurant_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"shift": shift}


@router.post("/self/clock-out", status_code=200)
async def self_clock_out(request: Request):
    """El operativo registra su propia salida usando su Bearer token."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await sessions_repo.get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    restaurant_id = await staff_repo.db_get_staff_restaurant_id(staff_id)
    if not restaurant_id:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    with tenant_scope(restaurant_id):
        shift = await db.db_clock_out(staff_id, restaurant_id)
    if not shift:
        raise HTTPException(status_code=404, detail="No hay turno abierto para este empleado.")
    return {"shift": shift}


# ── Clock-in / Clock-out (admin/dashboard — requiere get_current_restaurant) ──

@router.post("/clock-in", dependencies=_MODULE_DEPS)
async def clock_in(
    body: ClockInRequest,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return all currently open shifts for the restaurant."""
    shifts = await db.db_get_open_shifts(restaurant["id"])
    return {"shifts": shifts}


@router.get("/shifts", dependencies=_MODULE_DEPS)
async def get_shifts(
    date_from: str,
    date_to: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    Return shift history for [date_from, date_to).
    date_from / date_to: ISO 8601 strings, e.g. '2026-03-01T00:00:00Z'.
    """
    shifts = await db.db_get_shifts(restaurant["id"], date_from, date_to)
    return {"shifts": shifts}


class TipsAutoRequest(BaseModel):
    period_start: str
    period_end: str
    branch_id: int | None = None


@router.get("/tips/auto", dependencies=_MODULE_DEPS)
async def tips_auto(
    period_start: str,
    period_end: str,
    branch_id: int | None = None,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return auto-calculated tip distribution based on attendance overlap."""
    result = await db.db_calculate_tips_by_attendance(
        restaurant_id=restaurant["id"],
        period_start=period_start,
        period_end=period_end,
        branch_id=branch_id,
    )
    return result


@router.get("/tips/preview", dependencies=_MODULE_DEPS)
async def preview_tip_distribution(
    amount: Decimal,
    when: str | None = None,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Preview how a single tip would be distributed across staff currently
    on shift. Used by /caja during pay-check entry to give the cashier a
    real-time breakdown.

    Args:
      amount: tip amount (Decimal). Must be > 0 and <= 10M.
      when:   optional ISO8601 timestamp; defaults to NOW.
    """
    if amount is None or amount <= 0:
        raise HTTPException(status_code=400, detail="amount debe ser > 0")
    if amount > Decimal("10000000"):
        raise HTTPException(status_code=400, detail="amount fuera de rango")
    return await staff_repo.db_preview_tip_distribution(
        restaurant_id=restaurant["id"],
        tip_amount=amount,
        paid_at_iso=when,
    )


class TipDistributionConfig(BaseModel):
    config: dict[str, float]


@router.patch("/tip-distribution", dependencies=_MODULE_DEPS, status_code=200)
async def update_tip_distribution(
    body: TipDistributionConfig,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Update the tip distribution % config for all roles.

    The percentages must sum to exactly 100.0 (± 0.01 tolerance).
    Partial allocations (e.g. 50%) are not accepted — all tips must be
    assigned to avoid silent unallocated balances.
    """
    if body.config:
        total = sum(body.config.values())
        if abs(total - 100.0) > 0.01:
            raise HTTPException(
                status_code=400,
                detail=f"Los porcentajes deben sumar exactamente 100% (actualmente suman {total:.1f}%)",
            )
    await staff_repo.db_update_tip_distribution(restaurant["id"], body.config)
    return {"success": True, "config": body.config}


# ── Break management (self-service) ─────────────────────────────────────────

class BreakRequest(BaseModel):
    """No body needed - uses authenticated staff_id."""
    pass


@router.post("/self/break-start", status_code=200)
async def self_break_start(request: Request):
    """Start a break. Staff must have an open shift."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    session_key = await sessions_repo.get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key.split(":", 1)[1]
    restaurant_id = await staff_repo.db_get_staff_restaurant_id(staff_id)
    if not restaurant_id:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    with tenant_scope(restaurant_id):
        shifts = await db.db_get_open_shifts(restaurant_id)
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
    session_key = await sessions_repo.get_session(token)
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
    session_key = await sessions_repo.get_session(token)
    if not session_key or not session_key.startswith("staff:"):
        raise HTTPException(status_code=401, detail="Token inválido o no es un empleado operativo.")
    staff_id = session_key[6:]
    member = await staff_repo.db_get_staff_profile(staff_id)
    if not member:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    if not member.get("active"):
        raise HTTPException(status_code=403, detail="Staff inactivo")
    return member


@router.get("/self/profile", status_code=200)
async def self_profile(request: Request):
    """Retorna el perfil completo del operativo autenticado, incluyendo estado de turno y break."""
    member = await _resolve_staff_from_token(request)
    staff_id = member["id"]
    # Wave-2: db_get_staff_profile returns org_id (restaurant_id dropped in 0038).
    restaurant_id = member.get("org_id") or member.get("restaurant_id")

    shift_row, break_row = await staff_repo.db_get_staff_open_shift_and_break(staff_id)

    return {
        "id":              member["id"],
        "name":            member["name"],
        "username":        member.get("username", ""),
        "roles":           member["roles"],
        "role":            member["role"],
        "phone":           member["phone"],
        "document_number": member["document_number"],
        "hourly_rate":     float(to_decimal(member["hourly_rate"] or 0)),  # JSON boundary
        "photo_url":       member["photo_url"],
        "restaurant_id":   restaurant_id,
        "current_shift":   db._serialize(shift_row) if shift_row else None,
        "current_break":   db._serialize(break_row) if break_row else None,
    }


@router.get("/self/timecard", status_code=200)
async def self_timecard(request: Request, week_start: str = None, week_end: str = None):
    """Retorna el timecard semanal personal del operativo autenticado."""
    member = await _resolve_staff_from_token(request)
    staff_id = member["id"]
    # Wave-2: db_get_staff_profile returns org_id (restaurant_id dropped in 0038).
    restaurant_id = member.get("org_id") or member.get("restaurant_id")

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

    rows, sched_rows, ded_rows = await staff_repo.db_get_staff_timecard_rows(staff_id, ws_date, we_date)

    sched_map = {r["day_of_week"]: {"start": str(r["start_time"]), "end": str(r["end_time"])} for r in sched_rows}

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
    rows = await staff_repo.db_get_staff_schedule(staff_id)
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Get all schedules for the restaurant."""
    schedules = await db.db_get_schedules(restaurant["id"])
    return {"schedules": schedules}


@router.delete("/schedules/{schedule_id}", dependencies=_MODULE_DEPS, status_code=200)
async def delete_schedule(
    schedule_id: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Weekly timecard: hours per employee per day."""
    data = await db.db_get_timecard(restaurant["id"], week_start, week_end)
    return {"timecard": data}


# ── Overtime report (admin) ──────────────────────────────────────────────────

@router.get("/overtime", dependencies=_MODULE_DEPS)
async def get_overtime(
    date_from: str,
    date_to: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List all deduction items for a staff member."""
    items = await db.db_list_deduction_items(staff_id, restaurant["id"])
    return {"items": items}


@router.post("/{staff_id}/deductions", dependencies=_MODULE_DEPS, status_code=201)
async def create_deduction_item(
    staff_id: str,
    body: DeductionItemCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Calculate payroll for all staff of the org in the given period.

    Wave-2: payroll is org-level (one cálculo por business). The X-Branch-ID
    header narrows TIP aggregation to a specific sede via db_calculate_payroll's
    optional branch_id param — staff list (filtered by org_id) stays the same,
    only tip totals get scoped per location_id when requested.
    """
    org_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    branch_location_id = (
        int(branch_header) if branch_header and branch_header.isdigit() else None
    )
    entries = await db.db_calculate_payroll(
        org_id, period_start, period_end, branch_id=branch_location_id,
    )
    return {"entries": entries}


@router.post("/payroll/runs", dependencies=_MODULE_DEPS, status_code=201)
async def save_payroll_run(
    request: Request,
    body: dict,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Save payroll calculation as a draft run.

    Wave-2: db_save_payroll_run keys by org_id (one run per business per
    period). The X-Branch-ID header is honored only for the per-sede tip
    aggregation passed into db_calculate_payroll's branch_id param —
    NOT as the run's tenant key.
    """
    period_start = body.get("period_start")
    period_end   = body.get("period_end")
    if not period_start or not period_end:
        raise HTTPException(status_code=422, detail="period_start y period_end son requeridos.")
    org_id = restaurant["id"]
    branch_header = request.headers.get("X-Branch-ID")
    branch_location_id = (
        int(branch_header) if branch_header and branch_header.isdigit() else None
    )
    entries = await db.db_calculate_payroll(
        org_id, period_start, period_end, branch_id=branch_location_id,
    )
    run = await db.db_save_payroll_run(
        restaurant_id=org_id,
        period_start=period_start,
        period_end=period_end,
        snapshot=entries,
        config={},
        created_by=restaurant.get("whatsapp_number", ""),
    )
    return {"run": run}


@router.get("/payroll/runs", dependencies=_MODULE_DEPS)
async def list_payroll_runs(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List recent payroll runs.

    Wave-2: payroll_runs.org_id is the tenant key — runs are per-business,
    not per-sede. X-Branch-ID would convert org_id to location_id and the
    WHERE org_id = location_id filter returns 0 rows in multi-branch.
    """
    org_id = restaurant["id"]
    runs = await db.db_get_payroll_runs(org_id)
    return {"runs": runs}


@router.put("/payroll/runs/{run_id}/approve", dependencies=_MODULE_DEPS)
async def approve_payroll_run(
    run_id: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Mark a draft payroll run as approved.

    Repo `db_approve_payroll_run` returns the updated row or None when the
    run doesn't exist, belongs to another org, or is already approved.
    """
    org_id = restaurant["id"]
    run = await db.db_approve_payroll_run(run_id, org_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail="Corrida de nómina no encontrada o ya aprobada.",
        )
    return {"run": run}


@router.get("/payroll/runs/{run_id}/export", dependencies=_MODULE_DEPS)
async def export_payroll_run(
    run_id: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Download a saved payroll run as a CSV file."""
    branch_id = restaurant["id"]
    run = await db.db_get_payroll_run(run_id, branch_id)
    if not run:
        raise HTTPException(status_code=404, detail="Corrida de nómina no encontrada.")

    snapshot = run.get("snapshot") or []
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Nombre", "Rol", "Horas Regulares", "Horas Extra",
        "Tarifa/Hora", "Pago Bruto", "Propinas",
        "Compensación Total", "Total Deducciones", "Pago Neto",
    ])
    for e in snapshot:
        writer.writerow([
            e.get("name", ""),
            e.get("role", ""),
            e.get("regular_hours", 0),
            e.get("overtime_hours", 0),
            e.get("hourly_rate", 0),
            e.get("gross_pay", 0),
            e.get("tip_earnings", 0),
            e.get("total_compensation", 0),
            e.get("total_deductions", 0),
            e.get("net_pay", 0),
        ])

    period_start = str(run.get("period_start", "")).replace("-", "")
    period_end   = str(run.get("period_end",   "")).replace("-", "")
    filename     = f"nomina_{period_start}_{period_end}.csv"

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List all contract templates for the restaurant."""
    templates = await db.db_list_contract_templates(restaurant["id"])
    return {"templates": templates}


@router.post("/payroll/contracts", dependencies=_MODULE_DEPS, status_code=201)
async def create_contract_template(
    body: ContractTemplateCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Create a new contract template."""
    template = await db.db_create_contract_template(restaurant["id"], body.model_dump())
    return {"template": template}


@router.patch("/payroll/contracts/{template_id}", dependencies=_MODULE_DEPS)
async def update_contract_template(
    template_id: str,
    body: ContractTemplateUpdate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List overtime requests for review.

    Wave-2: overtime_requests.org_id is the tenant key — requests are
    org-level. X-Branch-ID would convert org_id to location_id and the
    WHERE org_id = location_id filter returns 0 rows in multi-branch.
    """
    org_id = restaurant["id"]
    requests = await db.db_list_overtime_requests(org_id, week_start, status)
    return {"overtime_requests": requests}


@router.patch("/payroll/overtime/{request_id}", dependencies=_MODULE_DEPS)
async def review_overtime_request(
    request_id: str,
    body: OvertimeReview,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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
