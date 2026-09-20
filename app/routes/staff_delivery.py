"""
app/routes/staff_delivery.py
==============================
Cashier "Domicilios" surface — the staff-app side of the web delivery/pickup
wave (docs/claude/delivery-web.md, chunk 4). BACKEND ONLY: no HTML/JS here —
the section's frontend page is a later chunk. app/services/staff_sections.py
(this same chunk) already grants the `delivery` section key to cashier roles
(caja/cashier/cajero) and every admin role (owner/admin/gerente).

Sede enforcement (docs/claude/delivery-web.md: "Staff only ever see their own
sede's orders"):
  - A cashier is scoped to THEIR OWN staff row's `location_id`. This relies
    on app/routes/deps.py::get_current_user actually selecting
    `staff.location_id` — found NOT wired at all (hardcoded to None) while
    building this chunk, and fixed in that same file. A cashier can never
    override this via a header; it is read from their own JWT-resolved
    staff row, never client input.
  - An admin role has no single "own" sede (they manage every location), so
    here they must pick ONE explicitly via the X-Location-ID header — the
    same convention already used by app/routes/deps.py::get_current_location
    for other multi-branch admin screens — validated against their own org
    before use.

Every transition below reuses the SQL writers already in
app/repositories/delivery_repo.py (chunk 1), extended in this chunk to also
filter by location_id, and publishes via
realtime.publish_delivery_status() (chunk 6), which fans out BOTH the
existing staff-only "order.updated" topic (kitchen/courier queues, unfiltered
/api/staff/stream) AND the diner-facing "delivery_order.updated" topic that
the customer's own /pedido/{code} status page listens on.
"""
from __future__ import annotations

import json as _json
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.repositories import delivery_repo
from app.routes.deps import get_current_user_scoped
from app.services import database as db
from app.services import realtime
from app.services.delivery import ALLOWED_PAYMENT_METHODS
from app.services.logging import get_logger
from app.services.staff_sections import ADMIN_ROLES, normalize_role

log = get_logger(__name__)

router = APIRouter(prefix="/api/staff/delivery", tags=["staff-delivery"])

_CASHIER_ROLES = frozenset({"caja", "cashier", "cajero"})
_COURIER_ROLES = frozenset({"domiciliario", "delivery"})

_WRONG_STATE_DETAIL = "El pedido ya no admite esta acción (cambió de estado)."
_NOT_OWNED_DETAIL = "Pedido no encontrado en tu sede."


# ── Auth / sede scoping ──────────────────────────────────────────────────────


def _roles_from_role_string(role_str: str | None) -> set[str]:
    return {normalize_role(r) for r in (role_str or "").split(",") if r.strip()}


def _staff_id_from_user(user: dict) -> str | None:
    """The staff UUID behind a `staff:<uuid>` JWT login, or None for a
    `users`-table admin/owner/gerente account (which has no `staff` row —
    accepted_by_staff_id / courier_staff_id stay NULL for those actions,
    which is honest given the schema's FK to staff(id))."""
    username = user.get("username") or ""
    if username.startswith("staff:"):
        return username.split(":", 1)[1]
    return None


async def delivery_scope(
    request: Request, user: dict = Depends(get_current_user_scoped),
) -> dict:
    """Resolve the caller's org_id + the ONE sede they may act on for every
    Domicilios endpoint below. Cashier, admin AND courier roles all resolve
    through here (chunk 7 extends this from cashier/admin-only so the
    courier's own "my orders" / en-route / delivered calls get the same
    sede enforcement) — per-endpoint role gates (`_require_cashier_or_admin`,
    `_require_can_transition`) then decide what each role may actually DO
    with that scope. Raises:
      403 — role has no access to Domicilios at all.
      400 — admin role, no X-Location-ID header.
      403 — admin's X-Location-ID does not belong to their own org.
      403 — cashier/courier role with no location_id on their own staff row.
    """
    roles = _roles_from_role_string(user.get("role"))
    org_id = user.get("org_id") or user.get("restaurant_id") or user.get("branch_id")
    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo resolver tu organización")
    org_id = int(org_id)

    is_admin = bool(roles & ADMIN_ROLES)
    is_cashier = bool(roles & _CASHIER_ROLES)
    is_courier = bool(roles & _COURIER_ROLES)
    if not (is_admin or is_cashier or is_courier):
        raise HTTPException(status_code=403, detail="No tienes acceso a Domicilios")

    if is_admin:
        header = request.headers.get("X-Location-ID", "").strip()
        if not header.isdigit():
            raise HTTPException(
                status_code=400,
                detail="Esta acción requiere el encabezado X-Location-ID con la sede a gestionar",
            )
        location_id = int(header)
        loc = await db.db_get_location_by_id(location_id)
        if not loc or int(loc.get("org_id") or -1) != org_id:
            raise HTTPException(status_code=403, detail="Esa sede no pertenece a tu organización")
    else:
        # Cashier or courier: their OWN staff row's sede — never client input.
        raw_location_id = user.get("location_id")
        if not raw_location_id:
            raise HTTPException(status_code=403, detail="Tu usuario no tiene una sede asignada")
        location_id = int(raw_location_id)

    return {
        "user": user,
        "org_id": org_id,
        "location_id": location_id,
        "staff_id": _staff_id_from_user(user),
        "is_admin": is_admin,
        "is_cashier": is_cashier,
        "is_courier": is_courier,
    }


def _require_cashier_or_admin(scope: dict) -> None:
    """Gate for the cashier-only actions (list/accept/reject/assign-courier/
    couriers) — a courier-only caller is refused even though `delivery_scope`
    resolved their sede for the endpoints they DO get (orders/mine,
    en-route, delivered)."""
    if not (scope["is_admin"] or scope["is_cashier"]):
        raise HTTPException(status_code=403, detail="Acción exclusiva de caja/administración")


def _require_can_transition(scope: dict, order: dict) -> None:
    """en-route/delivered are shared by cashier/admin (any order in their
    sede) AND the assigned courier (docs/claude/delivery-web.md, chunk 7:
    'the rider marks picked up / delivered'). A courier-only caller who is
    NOT the order's own courier_staff_id is refused — enforced here, not
    just by hiding the button client-side."""
    if scope["is_admin"] or scope["is_cashier"]:
        return
    if str(order.get("courier_staff_id") or "") != str(scope["staff_id"] or ""):
        raise HTTPException(status_code=403, detail="Este pedido no está asignado a ti")


# ── Pydantic bodies ──────────────────────────────────────────────────────────


class AcceptOrderRequest(BaseModel):
    eta_minutes: int = Field(..., ge=1, le=240)


class RejectOrderRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=300)


class AssignCourierRequest(BaseModel):
    courier_staff_id: str = Field(..., min_length=1, max_length=100)


# ── Response shaping ─────────────────────────────────────────────────────────


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value) -> float | None:
    return float(value) if value is not None else None  # JSON boundary


def _cashier_order_view(row: dict) -> dict:
    """Everything the cashier's Domicilios queue needs per order
    (docs/claude/delivery-web.md, chunk 4 item C.1)."""
    return {
        "id": row["id"],
        "status": row["status"],
        "order_type": row.get("order_type"),
        "customer_name": row.get("customer_name"),
        "customer_phone": row.get("customer_phone"),
        "address": row.get("address"),
        "delivery_lat": row.get("delivery_lat"),
        "delivery_lon": row.get("delivery_lon"),
        "items": row.get("items") or [],
        "notes": row.get("notes") or "",
        "subtotal": _money(row.get("subtotal")),
        "delivery_fee": _money(row.get("delivery_fee")),
        "tip_amount": _money(row.get("tip_amount")),
        "total": _money(row.get("total")),
        # Chunk 7: the courier section's "(pagado)/(cobrar)" label (extended
        # from the legacy courier.js, which already renders this) needs to
        # know whether the order was already paid (online/card) or is cash
        # still to collect — `paid` was already in _ORDER_FIELDS but never
        # surfaced here.
        "paid": bool(row.get("paid")),
        "paid_at": _iso(row.get("paid_at")),
        "paid_by_staff_id": (
            str(row["paid_by_staff_id"]) if row.get("paid_by_staff_id") else None
        ),
        "payment_method": row.get("payment_method"),
        "cash_change_for": _money(row.get("cash_change_for")),
        "proof_url": row.get("proof_url"),
        "public_code": row.get("public_code"),
        "scheduled_pickup_at": _iso(row.get("scheduled_pickup_at")),
        "estimated_minutes": row.get("estimated_minutes"),
        "accepted_at": _iso(row.get("accepted_at")),
        "accepted_by_staff_id": (
            str(row["accepted_by_staff_id"]) if row.get("accepted_by_staff_id") else None
        ),
        "rejected_at": _iso(row.get("rejected_at")),
        "rejection_reason": row.get("rejection_reason"),
        # Chunk 7: the cashier's "Cerrados hoy" group needs SOME terminal
        # timestamp for a cancelled order too (a customer can cancel before
        # acceptance) — these two were already selected by _ORDER_FIELDS but
        # never surfaced here.
        "cancelled_at": _iso(row.get("cancelled_at")),
        "cancelled_reason": row.get("cancelled_reason"),
        "courier_staff_id": str(row["courier_staff_id"]) if row.get("courier_staff_id") else None,
        "courier_assigned_at": _iso(row.get("courier_assigned_at")),
        "delivered_at": _iso(row.get("delivered_at")),
        "created_at": _iso(row.get("created_at")),
    }


async def _require_owned_order(scope: dict, order_id: str) -> dict:
    """404 if the order does not belong to the caller's org+sede at all —
    an authorization/visibility problem, never a 409 (see
    delivery_repo.db_get_delivery_order_for_sede's docstring for why the
    two are kept distinct). Callers still separately handle a 409 from the
    actual transition call for a genuine same-order state conflict."""
    order = await delivery_repo.db_get_delivery_order_for_sede(
        scope["org_id"], scope["location_id"], order_id,
    )
    if not order:
        raise HTTPException(status_code=404, detail=_NOT_OWNED_DETAIL)
    return order


def _roles_from_staff_row(row: dict) -> set[str]:
    raw = row.get("roles")
    roles: list = []
    if isinstance(raw, list):
        roles = raw
    elif isinstance(raw, str):
        try:
            parsed = _json.loads(raw)
            roles = parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            roles = []
    if not roles and row.get("role"):
        roles = [row["role"]]
    return {normalize_role(r) for r in roles}


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/orders")
async def list_delivery_orders(
    status: str | None = Query(default=None, max_length=300),
    scope: dict = Depends(delivery_scope),
):
    """The sede's FULL delivery/pickup queue, newest first, optionally
    filtered by a comma-separated status list. Cashier/admin only — a
    courier gets only their OWN assigned orders, at GET /orders/mine below,
    never this org-wide-per-sede view."""
    _require_cashier_or_admin(scope)
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    rows = await delivery_repo.db_list_delivery_orders(scope["org_id"], scope["location_id"], statuses)
    return {"orders": [_cashier_order_view(r) for r in rows]}


@router.get("/orders/mine")
async def list_my_delivery_orders(scope: dict = Depends(delivery_scope)):
    """The COURIER's own assigned orders in their own sede — never another
    courier's queue (docs/claude/delivery-web.md, chunk 7). Courier role
    only; cashier/admin have the broader GET /orders above."""
    if not scope["is_courier"] or scope["is_admin"] or scope["is_cashier"]:
        raise HTTPException(status_code=403, detail="Esta vista es solo para domiciliarios")
    if not scope["staff_id"]:
        raise HTTPException(status_code=403, detail="No se pudo resolver tu identidad de staff")
    rows = await delivery_repo.db_list_delivery_orders_for_courier(
        scope["org_id"], scope["location_id"], scope["staff_id"],
    )
    return {"orders": [_cashier_order_view(r) for r in rows]}


@router.get("/couriers")
async def list_couriers(scope: dict = Depends(delivery_scope)):
    """Active staff with a courier role (domiciliario/delivery) in the
    caller's own sede — feeds the cashier's 'Asignar domiciliario' picker.
    Cashier/admin only."""
    _require_cashier_or_admin(scope)
    rows = await delivery_repo.db_list_couriers_for_sede(scope["org_id"], scope["location_id"])
    return {"couriers": [{"id": r["id"], "name": r["name"]} for r in rows]}


@router.post("/orders/{order_id}/accept")
async def accept_delivery_order(
    order_id: str, body: AcceptOrderRequest, scope: dict = Depends(delivery_scope),
):
    """Accept with a manually typed ETA (minutes). Only from
    pendiente_aceptacion — this is what releases the ticket to the kitchen
    KDS (app/repositories/tables_repo.py::db_get_delivery_orders_for_cashier,
    fixed in this same chunk to actually exclude pendiente_aceptacion until
    now — see that function's docstring). Cashier/admin only."""
    _require_cashier_or_admin(scope)
    await _require_owned_order(scope, order_id)
    row = await delivery_repo.db_accept_order(
        scope["org_id"], order_id, scope["staff_id"], body.eta_minutes,
        location_id=scope["location_id"],
    )
    if not row:
        raise HTTPException(status_code=409, detail=_WRONG_STATE_DETAIL)
    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)


@router.post("/orders/{order_id}/reject")
async def reject_delivery_order(
    order_id: str, body: RejectOrderRequest, scope: dict = Depends(delivery_scope),
):
    """Reject with a mandatory reason. Only from pendiente_aceptacion.
    Cashier/admin only."""
    _require_cashier_or_admin(scope)
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="El motivo de rechazo es obligatorio")
    await _require_owned_order(scope, order_id)
    row = await delivery_repo.db_reject_order(
        scope["org_id"], order_id, reason, location_id=scope["location_id"],
    )
    if not row:
        raise HTTPException(status_code=409, detail=_WRONG_STATE_DETAIL)
    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)


@router.post("/orders/{order_id}/assign-courier")
async def assign_courier(
    order_id: str, body: AssignCourierRequest, scope: dict = Depends(delivery_scope),
):
    """Assign a rider. The courier MUST be an active staff member of the
    SAME org AND sede, with a courier role (domiciliario/delivery) —
    anything else is refused (docs/claude/delivery-web.md, chunk 4 item
    C.4). Not allowed once the order is in a terminal status.

    Ownership of the ORDER is checked first (404 if it's not even in the
    caller's org+sede) so a cashier can never learn anything about a
    courier-validation rule for an order they cannot see in the first
    place. Cashier/admin only."""
    _require_cashier_or_admin(scope)
    await _require_owned_order(scope, order_id)

    courier_id = body.courier_staff_id.strip()
    try:
        _uuid.UUID(courier_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="courier_staff_id inválido") from None

    courier = await delivery_repo.db_get_staff_for_courier_check(scope["org_id"], courier_id)
    if not courier or not courier.get("active", True):
        raise HTTPException(status_code=404, detail="Repartidor no encontrado en tu organización")
    if int(courier.get("location_id") or -1) != scope["location_id"]:
        raise HTTPException(status_code=422, detail="El repartidor no pertenece a esta sede")
    if not (_roles_from_staff_row(courier) & _COURIER_ROLES):
        raise HTTPException(status_code=422, detail="Ese miembro del staff no tiene rol de domiciliario")

    row = await delivery_repo.db_assign_courier(
        scope["org_id"], order_id, courier_id, location_id=scope["location_id"],
    )
    if not row:
        raise HTTPException(status_code=409, detail=_WRONG_STATE_DETAIL)
    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)


@router.post("/orders/{order_id}/en-route")
async def mark_en_route(order_id: str, scope: dict = Depends(delivery_scope)):
    """Move to en_camino — the rider picked it up. Only from
    en_preparacion/listo. Cashier/admin can act on any order in their sede;
    a courier may only act on an order assigned to THEM (chunk 7 —
    `_require_can_transition`, not just a hidden button)."""
    order = await _require_owned_order(scope, order_id)
    _require_can_transition(scope, order)
    row = await delivery_repo.db_mark_en_route(
        scope["org_id"], order_id, location_id=scope["location_id"],
    )
    if not row:
        raise HTTPException(status_code=409, detail=_WRONG_STATE_DETAIL)
    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)


class MarkPaidRequest(BaseModel):
    payment_method: str = Field(
        ..., min_length=1, max_length=30,
        description="Cómo pagó el cliente: efectivo, tarjeta, nequi o bancolombia",
    )


@router.post("/orders/{order_id}/mark-paid")
async def mark_order_paid(
    order_id: str, body: MarkPaidRequest, scope: dict = Depends(delivery_scope),
):
    """Record that the customer paid — cash at the door, the rider's card
    reader, or a transfer whose receipt the cashier just checked against the
    bank (proof_url is shown in the queue; approving it is this call).

    Web orders had no way to reach paid = TRUE at all: the only writer was
    the Wompi webhook, which is off. That left every delivery sale out of
    the owner's totals, since stats_repo sums `orders.total WHERE paid`.

    Same authorization as the en-route/delivered transitions: the sede's
    cashier or an admin for any order, plus the order's OWN assigned courier
    — the rider is who has the cash in their hand. `paid_by_staff_id` records
    which of them did it, and stays NULL for an owner/admin account (it has
    no `staff` row to point at — see _staff_id_from_user).

    409 when the order is already paid, cancelled or rejected: the repo's
    conditional UPDATE decides, so two tablets submitting at once cannot both
    win, and the second one is told rather than shown a false success.
    """
    method = body.payment_method.strip().lower()
    if method not in ALLOWED_PAYMENT_METHODS:
        raise HTTPException(
            status_code=400,
            detail=f"Método de pago no reconocido: {body.payment_method}",
        )

    order = await _require_owned_order(scope, order_id)
    _require_can_transition(scope, order)

    row = await delivery_repo.db_mark_order_paid(
        scope["org_id"], order_id,
        location_id=scope["location_id"],
        payment_method=method,
        staff_id=scope["staff_id"],
    )
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Este pedido ya estaba pagado o fue cancelado",
        )

    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)


@router.post("/orders/{order_id}/delivered")
async def mark_delivered(order_id: str, scope: dict = Depends(delivery_scope)):
    """Mark entregado (sets delivered_at). Only from en_camino/en_puerta
    (delivery) or en_preparacion/listo (pickup). Same cashier/admin-or-
    assigned-courier gate as en-route above."""
    order = await _require_owned_order(scope, order_id)
    _require_can_transition(scope, order)
    row = await delivery_repo.db_mark_delivered(
        scope["org_id"], order_id, location_id=scope["location_id"],
    )
    if not row:
        raise HTTPException(status_code=409, detail=_WRONG_STATE_DETAIL)
    await realtime.publish_delivery_status(scope["org_id"], scope["location_id"], order_id)
    return _cashier_order_view(row)
