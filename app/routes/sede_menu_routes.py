"""A sede's carta: its changes on top of the organization's menu (0093).

PM 2026-09-21: owner/admin edit the base carta and any sede's; a gerente
edits their own sede's. Who is which sede comes from
`deps.resolve_sede_filter` — owner/admin pick one with the sidebar's sede
selector, a gerente is pinned to theirs.

The base carta itself is still edited through `PUT /api/menu/update`.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.repositories import sede_menu_repo
from app.repositories.sede_menu_repo import SedeMenuError
from app.routes.deps import get_current_restaurant, get_current_user, resolve_sede_filter, roles_of
from app.services import sede_menu
from app.services.logging import get_logger
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter()

_CARTA_EDITORS = frozenset({"owner", "admin", "gerente"})


async def _editor_scope(request: Request) -> tuple[int, int]:
    """(org_id, location_id) the caller may edit the carta of, or 403/400."""
    user = await get_current_user(request)
    if not roles_of(user) & _CARTA_EDITORS:
        raise HTTPException(status_code=403, detail="Solo el dueño, un admin o el gerente cambian la carta")
    sede = resolve_sede_filter(request, user, admin_without_header="own")
    if not isinstance(sede, int):
        raise HTTPException(status_code=400, detail="Elige la sede cuya carta querés cambiar")
    restaurant = await get_current_restaurant(request)
    return int(restaurant["id"]), sede


def _refuse(exc: SedeMenuError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _override_out(row: dict) -> dict:
    price = row.get("price")
    return {
        "dish_name": row["dish_name"],
        "price": float(price) if price is not None else None,  # JSON boundary
        "hidden": bool(row["hidden"]),
    }


@router.get("/api/menu/sede")
async def get_sede_carta(request: Request):
    """Everything the editor needs: the base carta, this sede's changes to
    it, its own dishes, and the merged result the diner sees."""
    org_id, sede = await _editor_scope(request)
    with tenant_scope(org_id):
        base = await sede_menu_repo.db_get_org_menu(org_id)
        overrides = await sede_menu_repo.db_list_overrides(org_id, sede)
        own = await sede_menu_repo.db_list_own_dishes(org_id, sede)
    base_names = {
        sede_menu.dish_key(d.get("name"))
        for dishes in base.values() if isinstance(dishes, list)
        for d in dishes if isinstance(d, dict)
    }
    return {
        "location_id": sede,
        "base": base,
        "overrides": [
            {**_override_out(o), "orphaned": sede_menu.dish_key(o["dish_name"]) not in base_names}
            for o in overrides
        ],
        "own_dishes": [{"category": d["category"], "dish": d["dish"]} for d in own],
        "menu": sede_menu.apply_sede_changes(base, overrides, own),
    }


class OverrideIn(BaseModel):
    dish_name: str = Field(min_length=1, max_length=200)
    # None = the base price. A number = this sede's own price.
    price: float | str | None = None
    hidden: bool = False


@router.put("/api/menu/sede/override")
async def set_sede_override(body: OverrideIn, request: Request):
    org_id, sede = await _editor_scope(request)
    try:
        price = sede_menu.parse_price(body.price) if body.price not in (None, "") else None
    except ValueError:
        raise HTTPException(status_code=400, detail="El precio debe ser un número positivo, sin signos ni letras")
    with tenant_scope(org_id):
        base = await sede_menu_repo.db_get_org_menu(org_id)
        if not any(
            sede_menu.dish_key(d.get("name")) == sede_menu.dish_key(body.dish_name)
            for dishes in base.values() if isinstance(dishes, list)
            for d in dishes if isinstance(d, dict)
        ) and (price is not None or body.hidden):
            raise HTTPException(status_code=404, detail="Ese plato no está en la carta general")
        try:
            row = await sede_menu_repo.db_set_override(
                org_id, sede, body.dish_name, price=price, hidden=body.hidden,
            )
        except SedeMenuError as exc:
            raise _refuse(exc)
    log.info("sede_menu.override_set", org_id=org_id, location_id=sede, hidden=body.hidden,
             has_price=price is not None)
    return {"location_id": sede, "override": _override_out(row) if row else None}


class OwnDishIn(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    dish: dict
    previous_name: str | None = Field(default=None, max_length=200)


@router.put("/api/menu/sede/dish")
async def save_sede_dish(body: OwnDishIn, request: Request):
    org_id, sede = await _editor_scope(request)
    dish = dict(body.dish)
    try:
        dish["price"] = sede_menu.parse_price(dish.get("price"))
    except ValueError:
        raise HTTPException(status_code=400, detail="El precio debe ser un número positivo, sin signos ni letras")
    with tenant_scope(org_id):
        try:
            row = await sede_menu_repo.db_save_own_dish(
                org_id, sede, body.category, dish, previous_name=body.previous_name,
            )
        except SedeMenuError as exc:
            raise _refuse(exc)
    log.info("sede_menu.own_dish_saved", org_id=org_id, location_id=sede)
    return {"location_id": sede, "category": row["category"], "dish": row["dish"]}


@router.delete("/api/menu/sede/dish")
async def delete_sede_dish(dish_name: str, request: Request):
    org_id, sede = await _editor_scope(request)
    with tenant_scope(org_id):
        removed = await sede_menu_repo.db_delete_own_dish(org_id, sede, dish_name)
    if not removed:
        raise HTTPException(status_code=404, detail="Ese plato no existe en esta sede")
    log.info("sede_menu.own_dish_deleted", org_id=org_id, location_id=sede)
    return {"location_id": sede, "deleted": True}
