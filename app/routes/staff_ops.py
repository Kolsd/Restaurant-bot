"""
app/routes/staff_ops.py
=======================
"Configurar operación" — which screens a sede uses (app/services/ops_config.py).

    GET /api/staff/ops-config  — this sede's answers + its carta categories
    PUT /api/staff/ops-config  — save them (owner/admin any sede, gerente own)

The sede is the one the Staff App is working on: the header-picked sede for
owner/admin (or their only sede), the gerente's own sede. Same resolution as
/api/staff/sections, through `resolve_ops_sede`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.repositories import ops_config_repo
from app.routes.deps import get_current_user, may_span_locations, resolve_sede_filter
from app.services import database as db
from app.services import ops_config, plan_access, plans, sede_menu
from app.services.logging import get_logger
from app.services.staff_sections import ADMIN_ROLES, normalize_role
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/staff", tags=["staff"])


def can_configure(user: dict) -> bool:
    roles = {normalize_role(r) for r in (user.get("role") or "").split(",")}
    return bool(roles & ADMIN_ROLES)


async def resolve_ops_sede(request: Request, user: dict, org_id: int) -> int | None:
    """The sede the Staff App is operating, or None (a multi-sede owner who
    hasn't picked one). Never raises for a staff member without a sede — the
    caller decides what None means. Caller must be inside tenant_scope(org_id)."""
    try:
        loc = resolve_sede_filter(request, user, admin_without_header="own")
    except HTTPException:
        return None
    if loc is None and may_span_locations(user):
        sedes = await db.db_get_org_locations(org_id)
        if len(sedes or []) == 1:
            loc = int(sedes[0]["id"])
    return int(loc) if loc else None


class OpsConfigBody(BaseModel):
    bar: bool
    bar_categories: list[str] = Field(default_factory=list, max_length=ops_config.MAX_BAR_CATEGORIES)
    delivery: bool
    courier: bool
    waiter: bool


async def _context(request: Request) -> tuple[dict, int, int]:
    user = await get_current_user(request)
    if not can_configure(user):
        raise HTTPException(status_code=403, detail="Solo el dueño, un admin o el gerente configuran la operación.")
    org_id = int(user.get("org_id") or 0)
    if not org_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    with tenant_scope(org_id):
        loc = await resolve_ops_sede(request, user, org_id)
    if not loc:
        raise HTTPException(status_code=422, detail="Elige primero la sede que quieres configurar.")
    return user, org_id, loc


@router.get("/ops-config")
async def get_ops_config(request: Request):
    _, org_id, loc = await _context(request)
    with tenant_scope(org_id):
        raw = await ops_config_repo.db_get_ops_config(org_id, loc)
        if raw is None:
            raise HTTPException(status_code=404, detail="Sede no encontrada")
        menu = await sede_menu.get_sede_menu(org_id, loc)
        location = await db.db_get_location_by_id(loc)
    return {
        "location_id": loc,
        "location_name": (location or {}).get("name") or "",
        "config": ops_config.normalize(raw),
        "categories": [c for c, dishes in (menu or {}).items() if isinstance(dishes, list) and dishes],
        # Esencial has no /pedir: no Domicilios or Mis entregas to switch on.
        "delivery_in_plan": await plan_access.org_has_feature(org_id, plans.DELIVERY),
    }


@router.put("/ops-config")
async def put_ops_config(request: Request, body: OpsConfigBody):
    _, org_id, loc = await _context(request)
    with tenant_scope(org_id):
        menu = await sede_menu.get_sede_menu(org_id, loc)
        real_categories = {c for c, dishes in (menu or {}).items() if isinstance(dishes, list) and dishes}
        # Only categories this sede's carta really has; a stale or invented
        # name would route nothing anyway.
        bar_categories = [c for c in dict.fromkeys(body.bar_categories) if c in real_categories]
        if body.bar and not bar_categories:
            raise HTTPException(status_code=422, detail="Elige al menos una categoría de la carta que se prepare en el bar.")
        config = ops_config.normalize({
            "configured": True,
            "bar": body.bar,
            "bar_categories": bar_categories,
            "delivery": body.delivery,
            # Couriers deliver what Domicilios accepted: no Domicilios, no Mis entregas.
            "courier": body.courier and body.delivery,
            "waiter": body.waiter,
        })
        if not await ops_config_repo.db_set_ops_config(org_id, loc, config):
            raise HTTPException(status_code=404, detail="Sede no encontrada")
    return {"location_id": loc, "config": config}
