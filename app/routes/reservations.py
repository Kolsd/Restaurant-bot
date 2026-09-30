"""
Reservations API router.
Provides CRUD + status management + availability + stats for restaurant reservations.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from typing import Optional

from app.routes.deps import (
    require_auth, get_current_restaurant_scoped, require_module, require_plan_feature,
    get_current_user, may_span_locations, resolve_sede_filter,
)
from app.services import database as db
from app.services import plans
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
        # The plan first: "your plan does not include it" beats "module off".
        Depends(require_plan_feature(plans.RESERVATIONS)),
        Depends(require_module("module_reservations")),
    ],
)

# ── CREATE RESERVATION ───────────────────────────────────────────────────────


async def _sede_param(request: Request) -> str | int | None:
    """The sede filter for this caller, as the reservation repos expect it.

    Kept string-compatible because these three endpoints pass the value
    straight through to repo helpers that already accept `branch_id` as a
    str or int. An admin's explicit `?branch_id=` still wins; a non-admin's
    is ignored in favour of their own sede.
    """
    user = await get_current_user(request)
    if may_span_locations(user):
        explicit = request.query_params.get("branch_id")
        if explicit:
            return explicit
    return resolve_sede_filter(request, user, allow_all_sentinel=True)


@router.post("", status_code=201)
async def create_reservation(
    request: Request,
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

    sede = await _sede_param(request)
    location_id = int(sede) if sede and str(sede).isdigit() else restaurant.get("location_id")

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
            location_id=location_id,
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
    # Sede: an admin may name one (query param or header); everyone else gets
    # their own, never the whole org. Before this, any staff account could
    # read — or book into — another sede's reservations just by asking.
    branch_id = await _sede_param(request)

    if not date or not time or not guests_raw:
        raise HTTPException(
            status_code=422,
            detail="Query params 'date', 'time', and 'guests' are required",
        )

    try:
        guests = int(guests_raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="'guests' must be an integer")

    try:
        tables = await db.db_get_available_tables(
            date_str=date,
            time_str=time,
            guests=guests,
            org_id=int(restaurant["id"]),
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
    # Sede: an admin may name one (query param or header); everyone else gets
    # their own, never the whole org. Before this, any staff account could
    # read — or book into — another sede's reservations just by asking.
    branch_id = await _sede_param(request)

    if not period_start or not period_end:
        raise HTTPException(
            status_code=422,
            detail="Query params 'period_start' and 'period_end' are required",
        )

    try:
        stats = await db.db_get_reservation_stats(
            org_id=int(restaurant["id"]),
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
    # Sede: an admin may name one (query param or header); everyone else gets
    # their own, never the whole org. Before this, any staff account could
    # read — or book into — another sede's reservations just by asking.
    branch_id = await _sede_param(request)

    org_id = int(restaurant["id"])
    location_id = int(branch_id) if branch_id and str(branch_id).isdigit() else None

    try:
        if status:
            reservations = await db.db_get_reservations_by_status(
                org_id=org_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
                branch_id=location_id,
            )
        else:
            reservations = await db.db_get_reservations_range(
                date_from=date_from or "",
                date_to=date_to or "",
                org_id=org_id,
                location_id=location_id,
            )
    except Exception:
        log.exception(
            "reservations.list_error",
            org_id=org_id,
            status=status,
        )
        raise

    return {"reservations": reservations or []}


# ── ITEM ROUTES ──────────────────────────────────────────────────────────────


async def _verify_reservation_ownership(reservation_id: int, restaurant: dict) -> dict:
    """Fetch reservation and verify it belongs to this restaurant's org.

    All locations of an org share the same org_id, so any admin of the org
    can manage reservations across all its sedes.
    """
    reservation = await db.db_get_reservation_by_id(reservation_id)
    if not reservation:
        raise HTTPException(status_code=404, detail="Reservation not found")

    caller_org_id = restaurant.get("org_id") or restaurant.get("id")
    res_org_id = reservation.get("org_id")
    if res_org_id is None or caller_org_id is None or int(res_org_id) != int(caller_org_id):
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

    # The customer used to get a WhatsApp on cancellation; that channel was
    # retired 2026-09-25. The restaurant calls them from the phone they left.
    return reservation


class SeatReservationBody(BaseModel):
    table_id: str

    @field_validator("table_id")
    @classmethod
    def _table_id_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("table_id cannot be empty")
        return v


@router.post("/{reservation_id}/seat")
async def seat_reservation(
    reservation_id: int,
    body: SeatReservationBody,
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Mark a confirmed reservation as 'seated' and open a table_session.

    Closes DISCONNECT #9 (Reservations ↔ Salon) from PRODUCT_CONTEXT.md
    rule #13. Previously the host had to open the table manually
    (no link between the reservation and the table_session). Now:

      1. Customer arrives, host taps 'Cliente llegó' on the
         reservation card and picks the table from a quick selector.
      2. POST /api/reservations/{id}/seat with {table_id} →
         creates a table_session linking reservation.phone to the
         chosen table_id (with mesero auto-assigned via the existing
         db_create_table_session logic from DISCONNECT #2 fix).
      3. Reservation status transitions to 'seated' so the
         dashboard knows it's no longer "upcoming" / "confirmed".
      4. From then on, when the customer messages the bot the
         table_session is already open — bot picks them up on the
         right table without needing a QR scan.

    Idempotent: returns already_seated=true if the reservation is
    already in 'seated' status.
    """
    reservation = await _verify_reservation_ownership(reservation_id, restaurant)

    if reservation.get("status") == "cancelled":
        raise HTTPException(status_code=409, detail="La reserva está cancelada.")
    if reservation.get("status") == "seated":
        return {"already_seated": True, "reservation": reservation}

    phone = (reservation.get("phone") or "").strip()
    if not phone:
        raise HTTPException(
            status_code=422,
            detail="Reserva sin phone — no se puede abrir sesión sin número de contacto.",
        )

    # Verify the table exists AND belongs to this org (defense in depth — RLS
    # already filters but a clear 404 here helps the UI).
    table = await db.db_get_table_by_id(body.table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada.")
    caller_org_id = restaurant.get("org_id") or restaurant.get("id")
    if table.get("org_id") and caller_org_id and int(table["org_id"]) != int(caller_org_id):
        raise HTTPException(status_code=403, detail="La mesa no pertenece a este restaurante.")

    # Create the table_session. db_create_table_session auto-assigns the
    # least-loaded mesero at the location (DISCONNECT #2 fix).
    try:
        session = await db.db_create_table_session(
            phone=phone,
            org_id=int(caller_org_id),
            table_id=body.table_id,
            table_name=table.get("name") or body.table_id,
            location_id=table.get("location_id"),
        )
    except Exception:
        log.exception(
            "reservations.seat_create_session_failed",
            reservation_id=reservation_id,
            table_id=body.table_id,
            phone_hash=hash(phone),
        )
        raise HTTPException(status_code=500, detail="No se pudo abrir la sesión de mesa.")

    # Transition reservation status to 'seated'. Free-form status column,
    # no schema enforcement of allowed values — generic helper handles it.
    updated = await reservations_repo.db_update_reservation_status(
        reservation_id, status="seated"
    )

    log.info(
        "reservations.seated",
        reservation_id=reservation_id,
        table_id=body.table_id,
        session_id=session.get("id") if session else None,
    )
    return {"success": True, "session": session, "reservation": updated}


class ConfirmDepositManualBody(BaseModel):
    proof_media_id: Optional[str] = None
    notes: Optional[str] = None


@router.post("/{reservation_id}/confirm-deposit-manual")
async def confirm_deposit_manual(
    reservation_id: int,
    body: ConfirmDepositManualBody,
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    Caja-initiated manual confirmation of a reservation deposit.

    Mirror of the Wompi callback path for reservations (same flow used when
    the customer paid via screenshot-based method: nequi, transferencia, etc.
    and there is no automated callback to expect).

    Calls db_confirm_deposit_and_reservation atomically so the deposit row and
    the reservations row move together — eliminating the partial-state window
    that would leave a paid deposit bound to a pending reservation.

    Idempotent: returns 200 with `already_confirmed: true` if the deposit is
    already paid (caja double-click safety).
    """
    from app.repositories import reservation_deposits_repo as deposits_repo
    from datetime import datetime as _dt, timezone as _tz

    # Ownership (shared helper — fails closed on cross-org access)
    reservation = await _verify_reservation_ownership(reservation_id, restaurant)

    if reservation.get("status") == "cancelled":
        raise HTTPException(
            status_code=409,
            detail="La reserva está cancelada. Si el cliente pagó, se requiere reembolso manual.",
        )

    # Resolve the acting user for audit (request.state or direct token read —
    # get_current_restaurant_scoped already validated the token, but didn't
    # expose a user id. Use require_auth's session for the id if available.)
    user_id = None
    try:
        session = getattr(request.state, "session", None)
        if session:
            user_id = session.get("user_id") or session.get("id")
    except Exception:
        user_id = None

    manual_tx_id = f"manual:{user_id or 'caja'}:{_dt.now(_tz.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z"

    result = await deposits_repo.db_confirm_deposit_and_reservation(
        reservation_id, manual_tx_id
    )

    if result is None:
        # Either no pending deposit OR already paid. Distinguish by reloading.
        reservation_fresh = await db.db_get_reservation_by_id(reservation_id)
        if reservation_fresh and reservation_fresh.get("deposit_paid"):
            return {
                "success": True,
                "already_confirmed": True,
                "reservation_id": reservation_id,
            }
        raise HTTPException(
            status_code=404,
            detail="No hay depósito pendiente para esta reserva",
        )

    log.info(
        "reservations.deposit_manual_confirmed",
        reservation_id=reservation_id,
        user_id=user_id,
        proof_media_id=body.proof_media_id,
        notes=(body.notes or "")[:200],
        transaction_id=manual_tx_id,
    )

    return {
        "success": True,
        "reservation_id": reservation_id,
        "deposit": result.get("deposit"),
        "reservation": result.get("reservation"),
    }


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
