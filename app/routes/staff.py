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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from passlib.context import CryptContext

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
    name:     str       = Field(..., min_length=1, max_length=100)
    role:     str       = Field("mesero", pattern=r"^(mesero|cocina|bar|caja|gerente|domiciliario|otro)$")
    roles:    list[str] = Field(default_factory=list)
    password: str       = Field(..., min_length=4, max_length=100)
    phone:    str       = Field("", max_length=30)


class StaffUpdate(BaseModel):
    name:     str | None       = Field(None, min_length=1, max_length=100)
    role:     str | None       = Field(None, pattern=r"^(mesero|cocina|bar|caja|gerente|domiciliario|otro)$")
    roles:    list[str] | None = None
    password: str | None       = Field(None, min_length=4, max_length=100)
    phone:    str | None       = Field(None, max_length=30)
    active:   bool | None      = None


class StaffPinLoginRequest(BaseModel):
    restaurant_id: int
    name: str = Field(..., min_length=1, max_length=100)
    pin:  str = Field(..., min_length=4, max_length=100)


def _staff_redirect(roles: list) -> str:
    """Return the best landing page URL for the given role set."""
    if "gerente" in roles:
        return "/dashboard"
    if "cocina" in roles:
        return "/cocina"
    if "domiciliario" in roles:
        return "/domiciliario"
    # mesero, caja, bar, otro → POS / dashboard
    return "/dashboard"


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
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return all staff members (active and inactive) for the restaurant."""
    staff = await db.db_get_staff(restaurant["id"])
    return {"staff": staff}


@router.post("", dependencies=_MODULE_DEPS, status_code=201)
async def create_staff(
    body: StaffCreate,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Create a new staff member. Password is bcrypt-hashed before storage."""
    pin_hash = _pwd_ctx.hash(body.password)
    # If caller provides roles array, validate and use it; otherwise derive from role
    roles = [r for r in body.roles if r in _VALID_ROLES] if body.roles else [body.role]
    if not roles:
        roles = [body.role]
    member = await db.db_create_staff(
        restaurant_id=restaurant["id"],
        name=body.name,
        role=roles[0],
        pin_hash=pin_hash,
        phone=body.phone,
        roles=roles,
    )
    return {"staff": member}


@router.post("/pin-login", status_code=200)
async def staff_pin_login(body: StaffPinLoginRequest):
    """
    Authenticate a staff member by PIN.

    No Bearer token required — this is the login endpoint for staff.
    Returns a session token + redirect URL based on the employee's roles.
    The token is valid for the same TTL as dashboard sessions (72 h).
    """
    member = await db.db_get_staff_for_pin_login(body.restaurant_id, body.name)
    if not member:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    if not _pwd_ctx.verify(body.pin, member["pin"]):
        raise HTTPException(status_code=401, detail="PIN incorrecto.")

    token = secrets.token_hex(32)
    # Store session with a staff-prefixed username so require_auth works
    # without colliding with dashboard user accounts.
    await db.db_save_session(token, f"staff:{member['id']}")

    roles = member.get("roles") or [member.get("role", "mesero")]
    return {
        "token": token,
        "roles": roles,
        "name": member["name"],
        "redirect": _staff_redirect(roles),
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
        patch["roles"] = [r for r in patch["roles"] if r in _VALID_ROLES]
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

@router.post("/tip-cut", dependencies=_MODULE_DEPS)
async def tip_cut(
    body: TipCutRequest,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Preview + save a tip distribution cut.

    Step 1: calculate proportional distribution from features.tip_distribution config.
    Step 2: persist the cut to tip_distributions.
    Returns the saved distribution with per-employee breakdown.
    """
    preview = await db.db_calculate_tip_pool(
        restaurant_id=restaurant["id"],
        period_start=body.period_start,
        period_end=body.period_end,
        total_tips=body.total_tips,
    )

    if not preview["entries"]:
        raise HTTPException(
            status_code=422,
            detail=(
                "No se encontraron turnos en el período seleccionado, "
                "o el restaurante no tiene configurado 'tip_distribution' en sus features."
            ),
        )

    saved = await db.db_save_tip_distribution(
        restaurant_id=restaurant["id"],
        period_start=body.period_start,
        period_end=body.period_end,
        total_tips=body.total_tips,
        distribution=preview["entries"],
        pct_config=preview["pct_config"],
        created_by=restaurant.get("whatsapp_number", ""),
    )
    return {
        "distribution": saved,
        "preview": preview,
    }


@router.get("/tip-distributions", dependencies=_MODULE_DEPS)
async def tip_distributions(
    restaurant: dict = Depends(get_current_restaurant),
):
    """Return the 20 most recent tip distribution cuts."""
    cuts = await db.db_get_tip_distributions(restaurant["id"])
    return {"distributions": cuts}
