"""
app/routes/location_delivery.py
=================================
Per-sede delivery/pickup configuration — the restaurant-facing config side of
the web delivery/pickup wave (docs/claude/delivery-web.md, chunk 8 "THE
RESTAURANT'S CONFIGURATION SIDE").

Until this chunk, `locations.delivery_config` (migration 0082) was writable
ONLY by `delivery_repo.db_set_location_delivery_config` — there was no UI and
no endpoint for a restaurant to set it, so every sede silently ran on the
legacy org-level `organizations.features` defaults
(app/services/delivery.get_delivery_config). `locations.phone` (migration
0083) had the same gap: writable through `db_update_location`, but with
nowhere in the UI to type it.

Admin roles only (app/services/staff_sections.ADMIN_ROLES = owner, admin,
gerente) — this is a business-config surface, not an operational staff
screen (unlike app/routes/staff_delivery.py's Domicilios queue, which is
cashier/courier/admin).

`owner` and `admin` are the SAME authorization level everywhere in the
product (PM decision 2026-09-20), so this file no longer keeps its own
narrower {owner, admin} set — it reuses ADMIN_ROLES, the one constant that
already defines "admin" for the staff app.

`gerente` was added by the same decision, but scoped: whoever runs the sede
is who decides whether it takes domicilios today, so a gerente may read and
write the config of THEIR OWN sede and no other. Owners and admins keep
access to every sede in the org. A gerente with no location_id on their
account is refused rather than defaulted to some sede — guessing which one
is exactly the org/location ambiguity rls-multitenant.md forbids.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.repositories import delivery_repo, restaurant_repo
from app.routes.deps import get_current_user
from app.services import database as db
from app.services import delivery as delivery_svc
from app.services import plan_access, plans
from app.services.logging import get_logger
from app.services.staff_sections import ADMIN_ROLES
from app.services.money import to_decimal
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/locations", tags=["location-delivery-config"])

# Sane upper/lower bounds — chunk-8 instructions: "radius > 0 and a sane
# upper bound", "prep minutes in a sane range". Colombia-scale delivery: no
# real restaurant covers more than 100km, and no kitchen quotes more than
# 4 hours of prep on a delivery/pickup order.
_MAX_RADIUS_KM = Decimal("100")
_MIN_PREP_MINUTES = 1
_MAX_PREP_MINUTES = 240


class LocationDeliveryConfigPatch(BaseModel):
    # Money/radius arrive as plain JSON numbers (`float`) — the JSON boundary
    # (bot-rules.md / CLAUDE.md: "Money: Decimal + services/money.py; float
    # only at the JSON edge"). Converted to Decimal in _validate_patch below,
    # never compared or stored as float.
    delivery_enabled: bool = False
    pickup_enabled: bool = False
    delivery_fee: float = Field(default=0.0)
    min_order: float = Field(default=0.0)
    radius_km: float = Field(default=5.0)
    prep_minutes: int = Field(default=30)
    payment_methods: list[str] = Field(default_factory=list)
    phone: str = Field("", max_length=30)


def _user_roles(user: dict) -> set[str]:
    return {r.strip().lower() for r in (user.get("role") or "").split(",") if r.strip()}


def _require_admin_role(user: dict) -> set[str]:
    """Reject anyone who is not owner/admin/gerente. Returns the role set so
    the caller can apply the gerente sede restriction without re-parsing."""
    roles = _user_roles(user)
    if not (roles & ADMIN_ROLES):
        raise HTTPException(
            status_code=403,
            detail="Acceso restringido a dueños, administradores o gerentes",
        )
    return roles


def _require_config_access(user: dict, location_id: int) -> None:
    """Authorize reading/writing ONE sede's delivery config.

    owner/admin  → any sede in their org.
    gerente only → their own sede, and only if their account has one.
    """
    roles = _require_admin_role(user)
    if roles & {"owner", "admin"}:
        return

    own = user.get("location_id")
    if own is None:
        raise HTTPException(
            status_code=403,
            detail="Tu usuario de gerente no tiene una sede asignada",
        )
    if int(own) != int(location_id):
        raise HTTPException(
            status_code=403,
            detail="Solo puedes configurar los domicilios de tu propia sede",
        )


def _resolve_org_id(user: dict) -> int:
    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una organización asignada")
    return int(org_id)


async def _owned_location_or_404(org_id: int, location_id: int) -> dict:
    """Fetch the location and verify it belongs to org_id — never trust the
    path parameter without this check (rls-multitenant.md: org_id/location_id
    are distinct integers, IDs can collide across tenants)."""
    with tenant_scope(org_id):
        location = await restaurant_repo.db_get_location_by_id(location_id)
    if not location or int(location.get("org_id") or -1) != org_id:
        raise HTTPException(status_code=404, detail="Sede no encontrada")
    return location


def _raw_location_config(location: dict) -> dict:
    cfg = location.get("delivery_config")
    return cfg if isinstance(cfg, dict) else {}


def _config_view(org: dict | None, location: dict) -> dict:
    """Effective values (chunk 1's resolver) plus which of them are actually
    set ON THIS SEDE versus inherited from the org/hardcoded default — the
    "make it clear which ones are inherited" requirement."""
    effective = delivery_svc.get_delivery_config(org, location)
    org_default = delivery_svc.get_org_default_config(org)
    raw_cfg = _raw_location_config(location)
    return {
        "effective": delivery_svc.delivery_config_to_json(effective),
        "org_default": delivery_svc.delivery_config_to_json(org_default),
        "overrides": {key: (key in raw_cfg) for key in effective},
    }


async def _build_response(org_id: int, location_id: int, location: dict) -> dict:
    with tenant_scope(org_id):
        org = await db.db_get_org_by_id(org_id)
    view = _config_view(org, location)
    view["location_id"] = location_id
    view["phone"] = location.get("phone") or ""
    view["public_link"] = f"/pedir/{(org or {}).get('slug') or ''}"
    # The effective values above are already off on a plan without delivery;
    # this tells the UI why.
    view["plan_allows_delivery"] = bool(org) and plans.has_feature(org, plans.DELIVERY)
    return view


def _validate_patch(body: LocationDeliveryConfigPatch) -> dict:
    """Returns the validated config dict (Decimal money/radius, normalized
    lower-cased payment methods) ready to hand to
    delivery_repo.db_set_location_delivery_config, or raises
    HTTPException(400) naming the first thing wrong — every value is
    checked, never just a status-code-only accept."""
    delivery_fee = to_decimal(body.delivery_fee)
    min_order = to_decimal(body.min_order)
    radius_km = to_decimal(body.radius_km)

    if delivery_fee < 0:
        raise HTTPException(status_code=400, detail="La tarifa de domicilio no puede ser negativa")
    if min_order < 0:
        raise HTTPException(status_code=400, detail="El pedido mínimo no puede ser negativo")
    if radius_km <= 0 or radius_km > _MAX_RADIUS_KM:
        raise HTTPException(
            status_code=400,
            detail=f"El radio de cobertura debe ser mayor a 0 y menor o igual a {_MAX_RADIUS_KM} km",
        )
    if not (_MIN_PREP_MINUTES <= body.prep_minutes <= _MAX_PREP_MINUTES):
        raise HTTPException(
            status_code=400,
            detail=f"El tiempo de preparación debe estar entre {_MIN_PREP_MINUTES} y {_MAX_PREP_MINUTES} minutos",
        )
    methods = sorted({str(m).strip().lower() for m in body.payment_methods if str(m).strip()})
    unknown = set(methods) - set(delivery_svc.ALLOWED_PAYMENT_METHODS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Método de pago no reconocido: {', '.join(sorted(unknown))}",
        )
    if not body.delivery_enabled and not body.pickup_enabled:
        raise HTTPException(
            status_code=400,
            detail="Activa domicilio o recogida para que esta sede reciba pedidos web",
        )
    return {
        "delivery_enabled": body.delivery_enabled,
        "pickup_enabled": body.pickup_enabled,
        "delivery_fee": delivery_fee,
        "min_order": min_order,
        "radius_km": radius_km,
        "prep_minutes": body.prep_minutes,
        "payment_methods": methods,
    }


@router.get("/{location_id}/delivery-config")
async def get_location_delivery_config(location_id: int, request: Request):
    user = await get_current_user(request)
    _require_config_access(user, location_id)
    org_id = _resolve_org_id(user)

    location = await _owned_location_or_404(org_id, location_id)
    return await _build_response(org_id, location_id, location)


@router.put("/{location_id}/delivery-config")
async def set_location_delivery_config(
    location_id: int, body: LocationDeliveryConfigPatch, request: Request,
):
    user = await get_current_user(request)
    _require_config_access(user, location_id)
    org_id = _resolve_org_id(user)

    location = await _owned_location_or_404(org_id, location_id)
    config = _validate_patch(body)
    if config.get("delivery_enabled") or config.get("pickup_enabled"):
        with tenant_scope(org_id):
            await plan_access.require_feature(org_id, plans.DELIVERY)

    phone = body.phone.strip()
    with tenant_scope(org_id):
        updated = await delivery_repo.db_set_location_delivery_config(org_id, location_id, config)
        if updated and phone != (location.get("phone") or ""):
            await restaurant_repo.db_update_location(location_id, phone=phone)

    if not updated:
        raise HTTPException(status_code=404, detail="Sede no encontrada")

    refreshed = await _owned_location_or_404(org_id, location_id)
    log.info("location_delivery.config_updated", org_id=org_id, location_id=location_id)
    return await _build_response(org_id, location_id, refreshed)
