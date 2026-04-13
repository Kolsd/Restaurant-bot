"""
Reservations API router.
Provides CRUD + status management + availability + stats for restaurant reservations.
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse

from app.routes.deps import require_auth, get_current_restaurant, require_module
from app.services import database as db
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/reservations",
    tags=["reservations"],
    dependencies=[
        Depends(require_auth),
        Depends(require_module("module_reservations")),
    ],
)

# ── STATIC ROUTES (must come before /{reservation_id}) ──────────────────────


@router.get("/availability")
async def check_availability(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant),
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
    restaurant: dict = Depends(get_current_restaurant),
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
    restaurant: dict = Depends(get_current_restaurant),
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
    """Fetch reservation and verify it belongs to this restaurant. Raises 404 if not found, 403 if not owned."""
    reservation = await db.db_get_reservation_by_id(reservation_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")
    bot_number = restaurant.get("whatsapp_number") or restaurant.get("bot_number", "")
    if reservation.get("bot_number") and reservation["bot_number"] != bot_number:
        raise HTTPException(status_code=403, detail="Reservation does not belong to this restaurant")
    return reservation


@router.get("/{reservation_id}")
async def get_reservation(
    reservation_id: int,
    restaurant: dict = Depends(get_current_restaurant),
):
    """Fetch a single reservation by ID."""
    reservation = await _verify_reservation_ownership(reservation_id, restaurant)
    return reservation


@router.put("/{reservation_id}/status")
async def update_reservation_status(
    request: Request,
    reservation_id: int,
    restaurant: dict = Depends(get_current_restaurant),
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
    restaurant: dict = Depends(get_current_restaurant),
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
