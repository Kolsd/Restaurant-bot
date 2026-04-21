"""
Reservations API router.
Provides CRUD + status management + availability + stats for restaurant reservations.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional

from app.routes.deps import require_auth, get_current_restaurant_scoped, require_module
from app.services import database as db
from app.services.logging import get_logger
from app.repositories import reservations_repo

log = get_logger(__name__)


class CreateReservationBody(BaseModel):
    customer_name: str
    customer_phone: Optional[str] = None
    party_size: int
    date: str        # YYYY-MM-DD
    time: str        # HH:MM
    notes: Optional[str] = None
    table_id: Optional[int] = None
    source: str = "manual"

    @field_validator("customer_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("customer_name cannot be empty")
        return v.strip()

    @field_validator("party_size")
    @classmethod
    def party_size_range(cls, v: int) -> int:
        if v < 1 or v > 20:
            raise ValueError("party_size must be between 1 and 20")
        return v

    @field_validator("date")
    @classmethod
    def date_format(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date must be in YYYY-MM-DD format")
        return v

    @field_validator("time")
    @classmethod
    def time_format(cls, v: str) -> str:
        # Accept HH:MM or HH:MM:SS
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                datetime.strptime(v, fmt)
                return v[:5]  # normalise to HH:MM
            except ValueError:
                continue
        raise ValueError("time must be in HH:MM format")

router = APIRouter(
    prefix="/api/reservations",
    tags=["reservations"],
    dependencies=[
        Depends(require_auth),
        Depends(require_module("module_reservations")),
    ],
)

# ── CREATE RESERVATION ───────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_reservation(
    body: CreateReservationBody,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Create a new reservation. Status defaults to 'pending'."""
    # Validate that the reservation datetime is in the future
    try:
        reservation_dt = datetime.strptime(
            f"{body.date} {body.time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Cannot parse reservation date/time",
        )

    now_utc = datetime.now(tz=timezone.utc)
    if reservation_dt <= now_utc:
        raise HTTPException(
            status_code=400,
            detail="Reservation date/time must be in the future",
        )

    bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number") or ""

    try:
        reservation = await reservations_repo.db_create_reservation(
            customer_name=body.customer_name,
            date_str=body.date,
            time_str=body.time,
            party_size=body.party_size,
            customer_phone=body.customer_phone,
            notes=body.notes,
            table_id=body.table_id,
            source=body.source,
            bot_number=bot_number,
        )
    except Exception:
        log.exception(
            "reservations.create_error",
            customer_name=body.customer_name,
            date=body.date,
            time=body.time,
        )
        raise

    log.info(
        "reservations.created",
        reservation_id=reservation.get("id"),
        party_size=body.party_size,
    )
    return reservation


# ── STATIC ROUTES (must come before /{reservation_id}) ──────────────────────


@router.get("/availability")
async def check_availability(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Check which tables are available for a given date/time/party size."""
    date = request.query_params.get("date")
    time = request.query_params.get("time")
    guests_raw = request.query_params.get("guests")
    branch_id = request.query_params.get("branch_id") or request.headers.get("X-Branch-ID")

    if not date or not time or not guests_raw:
        raise HTTPException(
            status_code=422,
            detail="Query params 'date', 'time', and 'guests' are required",
        )

    try:
        guests = int(guests_raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="'guests' must be an integer")

    bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number")

    try:
        tables = await db.db_get_available_tables(
            date_str=date,
            time_str=time,
            guests=guests,
            bot_number=bot_number,
            branch_id=int(branch_id) if branch_id and str(branch_id).isdigit() else None,
        )
    except Exception:
        log.exception("reservations.availability_error", date=date, time=time, guests=guests)
        raise

    return {"available_tables": tables or []}


@router.get("/stats")
async def reservation_stats(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return aggregated reservation statistics for a given period."""
    period_start = request.query_params.get("period_start")
    period_end = request.query_params.get("period_end")
    branch_id = request.query_params.get("branch_id") or request.headers.get("X-Branch-ID")

    if not period_start or not period_end:
        raise HTTPException(
            status_code=422,
            detail="Query params 'period_start' and 'period_end' are required",
        )

    bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number")

    try:
        stats = await db.db_get_reservation_stats(
            bot_number=bot_number,
            period_start=period_start,
            period_end=period_end,
            branch_id=int(branch_id) if branch_id and str(branch_id).isdigit() else None,
        )
    except Exception:
        log.exception(
            "reservations.stats_error",
            period_start=period_start,
            period_end=period_end,
        )
        raise

    return stats or {}


# ── COLLECTION ROUTES ────────────────────────────────────────────────────────


@router.get("")
async def list_reservations(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List reservations filtered by date range and optionally by status."""
    date_from = request.query_params.get("date_from")
    date_to = request.query_params.get("date_to")
    status = request.query_params.get("status")
    branch_id = request.query_params.get("branch_id") or request.headers.get("X-Branch-ID")

    bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number")

    try:
        if status:
            reservations = await db.db_get_reservations_by_status(
                bot_number=bot_number,
                status=status,
                date_from=date_from,
                date_to=date_to,
                branch_id=int(branch_id) if branch_id and str(branch_id).isdigit() else None,
            )
        else:
            reservations = await db.db_get_reservations_range(
                date_from=date_from or "",
                date_to=date_to or "",
                bot_number=bot_number,
            )
    except Exception:
        log.exception(
            "reservations.list_error",
            bot_number=bot_number,
            status=status,
        )
        raise

    return {"reservations": reservations or []}


# ── ITEM ROUTES ──────────────────────────────────────────────────────────────


async def _verify_reservation_ownership(reservation_id: int, restaurant: dict) -> dict:
    """Fetch reservation and verify it belongs to this restaurant's org.

    Wave-2: bot_number comparison fails for branch reservations viewed by the
    matriz admin (different whatsapp_number per sede).  Use org_id instead —
    all locations of an org share the same org_id so any admin of the org can
    manage reservations across all its sedes.
    """
    reservation = await db.db_get_reservation_by_id(reservation_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")

    # Prefer org_id comparison; fall back to bot_number for legacy rows without org_id.
    caller_org_id = restaurant.get("org_id") or restaurant.get("id")
    res_org_id = reservation.get("org_id")

    if res_org_id is not None and caller_org_id is not None:
        if int(res_org_id) != int(caller_org_id):
            raise HTTPException(status_code=403, detail="Reservation does not belong to this restaurant")
    else:
        # Legacy fallback: bot_number check
        bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number", "")
        if reservation.get("bot_number") and reservation["bot_number"] != bot_number:
            raise HTTPException(status_code=403, detail="Reservation does not belong to this restaurant")

    return reservation


@router.get("/{reservation_id}")
async def get_reservation(
    reservation_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Fetch a single reservation by ID."""
    reservation = await _verify_reservation_ownership(reservation_id, restaurant)
    return reservation


@router.put("/{reservation_id}/status")
async def update_reservation_status(
    request: Request,
    reservation_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Update reservation status. Routes to the appropriate DB helper per status value."""
    await _verify_reservation_ownership(reservation_id, restaurant)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")

    status = (body.get("status") or "").strip().lower()
    reason = body.get("reason", "")

    valid_statuses = {"confirmed", "cancelled", "completed", "no_show"}
    if not status or status not in valid_statuses:
        raise HTTPException(
            status_code=422,
            detail=f"'status' must be one of: {', '.join(sorted(valid_statuses))}",
        )

    try:
        if status == "confirmed":
            reservation = await db.db_confirm_reservation(reservation_id)
        elif status == "cancelled":
            reservation = await db.db_cancel_reservation(reservation_id, reason=reason)
        elif status == "no_show":
            reservation = await db.db_mark_no_show(reservation_id)
        else:  # completed
            reservation = await db.db_update_reservation_status(reservation_id, status=status)
    except Exception:
        log.exception(
            "reservations.status_update_error",
            reservation_id=reservation_id,
            status=status,
        )
        raise

    if not reservation:
        return JSONResponse({"detail": "Reservation not found"}, status_code=404)

    return reservation


@router.put("/{reservation_id}/assign-table")
async def assign_table(
    request: Request,
    reservation_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Assign a table to an existing reservation."""
    await _verify_reservation_ownership(reservation_id, restaurant)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")

    table_id = body.get("table_id")
    if not table_id:
        raise HTTPException(status_code=422, detail="'table_id' is required")

    try:
        reservation = await db.db_assign_table_to_reservation(
            reservation_id=reservation_id,
            table_id=table_id,
        )
    except Exception:
        log.exception(
            "reservations.assign_table_error",
            reservation_id=reservation_id,
            table_id=table_id,
        )
        raise

    if not reservation:
        return JSONResponse({"detail": "Reservation not found"}, status_code=404)

    return reservation
