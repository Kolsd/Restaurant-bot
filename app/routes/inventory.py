"""Inventory - stock, recipes and food cost.

Stock is PER SEDE (PM 2026-09-20: "el inventario es uno por sede"). Every
listing is filtered by the caller's sede through
`app/routes/deps.py::resolve_sede_filter`, creating an item requires naming
one, and stock moves between sedes with
POST /api/inventory/{item_id}/transfer.

Recipes (`dish_recipes`) and food costs stay ORG-level on purpose: a dish is
made the same way in every sede. Only the stock it consumes is local.
"""
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from app.services import database as db
from app.repositories import inventory_repo
from app.repositories.orders_repo import InsufficientStockError
from app.routes.deps import (
    require_auth, get_current_restaurant_scoped, get_current_user,
    may_span_locations, resolve_sede_filter,
)
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


async def _sede_for_read(request: Request) -> int | None:
    """The sede whose stock the caller may list. None = every sede, which
    only owner/admin get."""
    user = await get_current_user(request)
    sede = resolve_sede_filter(request, user)
    return sede if isinstance(sede, int) else None


async def _sede_for_write(request: Request, org_id: int, requested: int | None) -> int:
    """The sede a new item belongs to, or 400.

    An owner/admin manages several sedes, so they must SAY which one - the
    sidebar's "todas las sedes" is not an answer to "where does this stock
    live" (PM: "si el owner quiere anadir inventario debera escoger la sede
    primero con un selector de sedes"). Anyone else writes to their own sede
    and a `location_id` in the body is ignored rather than trusted.
    """
    user = await get_current_user(request)
    if not may_span_locations(user):
        own = resolve_sede_filter(request, user)
        return int(own)

    target = requested if requested is not None else resolve_sede_filter(request, user)
    if not isinstance(target, int):
        raise HTTPException(
            status_code=400,
            detail="Elegi la sede a la que pertenece este producto antes de guardarlo",
        )
    owns = await db.db_get_location_by_id(int(target))
    if not owns or int(owns.get("org_id") or -1) != int(org_id):
        raise HTTPException(status_code=404, detail="Sede no encontrada")
    return int(target)


async def _owned_item_or_404(request: Request, item_id: int, org_id: int) -> dict:
    """Fetch an item and refuse it unless the caller's sede may touch it.

    The org_id check alone (all this used to do) let a cook at sede A edit,
    delete or adjust sede B's stock - same tenant, different fridge.
    """
    existing = await db.db_get_inventory_item(item_id)
    if not existing or existing.get("org_id") != org_id:
        log.warning("inventory.idor_attempt", item_id=item_id, org_id=org_id)
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    sede = await _sede_for_read(request)
    item_sede = existing.get("location_id")
    # An unassigned row (location_id NULL) stays reachable from any sede -
    # it is visible in every stock list, so it must be editable too.
    if sede is not None and item_sede is not None and int(item_sede) != sede:
        log.warning(
            "inventory.cross_sede_attempt",
            item_id=item_id, org_id=org_id,
            item_location_id=item_sede, caller_sede=sede,
        )
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return existing


class InventoryItemCreate(BaseModel):
    name: str
    unit: str = "unidades"          # unidades, kg, litros, etc.
    current_stock: float
    min_stock: float = 0            # alert threshold
    linked_dishes: List[str] = []   # exact menu dish names
    cost_per_unit: float = 0        # cost per unit (optional)
    # The sede this stock lives in. owner/admin must send it; for anyone else
    # it is ignored in favour of their own sede.
    location_id: Optional[int] = None


class InventoryItemUpdate(BaseModel):
    name: Optional[str] = None
    unit: Optional[str] = None
    current_stock: Optional[float] = None
    min_stock: Optional[float] = None
    linked_dishes: Optional[List[str]] = None
    cost_per_unit: Optional[float] = None


class StockAdjustment(BaseModel):
    quantity: float
    reason: str = "ajuste_manual"   # ajuste_manual, compra, merma


@router.get("/api/inventory")
async def get_inventory(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Stock of the caller's sede (every sede for owner/admin)."""
    sede = await _sede_for_read(request)
    items = await db.db_get_inventory(restaurant["id"], location_id=sede)
    return {"items": items, "location_id": sede}


@router.post("/api/inventory")
async def create_inventory_item(
    request: Request,
    body: InventoryItemCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Creates a stock item IN a sede. 400 if an owner has not picked one."""
    location_id = await _sede_for_write(request, restaurant["id"], body.location_id)
    item = await db.db_create_inventory_item(
        restaurant_id=restaurant["id"],
        name=body.name,
        unit=body.unit,
        current_stock=body.current_stock,
        min_stock=body.min_stock,
        linked_dishes=body.linked_dishes,
        cost_per_unit=body.cost_per_unit,
        location_id=location_id,
    )
    return {"success": True, "item": item}


@router.put("/api/inventory/{item_id}")
async def update_inventory_item(
    request: Request,
    item_id: int,
    body: InventoryItemUpdate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Updates an inventory product"""
    await _owned_item_or_404(request, item_id, restaurant["id"])
    item = await db.db_update_inventory_item(item_id, body.dict(exclude_none=True))
    if not item:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return {"success": True, "item": item}


@router.delete("/api/inventory/{item_id}")
async def delete_inventory_item(
    request: Request,
    item_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Deletes an inventory product"""
    await _owned_item_or_404(request, item_id, restaurant["id"])
    await db.db_delete_inventory_item(item_id)
    return {"success": True}


@router.post("/api/inventory/{item_id}/adjust")
async def adjust_stock(
    request: Request,
    item_id: int,
    body: StockAdjustment,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Manually adjusts stock (restock, shrinkage, etc.) in the caller's sede."""
    await _owned_item_or_404(request, item_id, restaurant["id"])
    result = await db.db_adjust_inventory_stock(
        item_id=item_id,
        quantity_delta=body.quantity,
        reason=body.reason,
        restaurant_id=restaurant["id"]
    )
    if not result:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return {"success": True, "item": result}


class StockTransfer(BaseModel):
    to_location_id: int = Field(..., description="Sede que recibe el producto")
    quantity: float = Field(..., gt=0, description="Cuanto se traslada")
    note: str = Field("", max_length=200)


@router.post("/api/inventory/{item_id}/transfer")
async def transfer_stock(
    request: Request,
    item_id: int,
    body: StockTransfer,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Move stock of one product to another sede of the same org.

    PM 2026-09-20: "se puede hacer intercambios de inventario por sede".

    Who may do it follows the same line as everything else here: the SOURCE
    must be a sede the caller may act on, so a gerente can send stock out of
    their own sede but cannot reach into another one and pull stock from it.
    The destination can be any sede of the org - receiving is not a privilege.
    `_owned_item_or_404` enforces the source side.

    The destination row is the same product at the other sede, matched by
    name, created there if it does not exist yet (see
    inventory_repo.db_transfer_inventory). Both sides get an
    `inventory_history` entry, so each sede's movement log explains where the
    stock went or came from.

    409 - not enough stock to send. 400 - same sede, or a destination that is
    not yours. Never a partial move: the repo does it in one transaction with
    the source row locked.
    """
    await _owned_item_or_404(request, item_id, restaurant["id"])
    try:
        result = await inventory_repo.db_transfer_inventory(
            org_id=restaurant["id"],
            item_id=item_id,
            to_location_id=body.to_location_id,
            quantity=body.quantity,
            note=body.note,
        )
    except inventory_repo.SedeTransferError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except InsufficientStockError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    return {"success": True, **result}


@router.get("/api/inventory/{item_id}/history")
async def get_stock_history(
    request: Request,
    item_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Stock movement history"""
    await _owned_item_or_404(request, item_id, restaurant["id"])
    history = await db.db_get_inventory_history(item_id)
    return {"history": history}


@router.get("/api/inventory/alerts")
async def get_inventory_alerts(
    request: Request,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Products with low or depleted stock, in the caller's sede."""
    sede = await _sede_for_read(request)
    alerts = await db.db_get_inventory_alerts(restaurant["id"], location_id=sede)
    return {"alerts": alerts}


@router.get("/api/inventory/menu-items")
async def get_menu_items_for_linking(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Returns all menu dishes for the linking selector"""
    menu = await db.db_get_menu(restaurant["whatsapp_number"]) or {}
    dishes = []
    for category, items in menu.items():
        for item in items:
            dishes.append({"name": item.get("name", ""), "category": category})
    return {"dishes": dishes}


# ── RECIPES (PHASE 4) ────────────────────────────────────────────

class RecipeLine(BaseModel):
    ingredient_id: int
    quantity: float


class RecipeUpsert(BaseModel):
    dish_name: str
    lines: List[RecipeLine]


@router.get("/api/inventory/recipes")
async def get_all_recipes(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Lists all recipes with food cost per dish."""
    recipes = await db.db_get_all_recipes(restaurant["id"])
    return {"recipes": recipes}


@router.get("/api/inventory/recipes/{dish_name}")
async def get_recipe(
    dish_name: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Returns a dish's ingredient lines."""
    lines = await db.db_get_dish_recipe(restaurant["id"], dish_name)
    return {"dish_name": dish_name, "lines": lines}


@router.post("/api/inventory/recipes")
async def upsert_recipe(
    body: RecipeUpsert,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Creates or replaces a dish's full recipe."""
    if not body.dish_name.strip():
        raise HTTPException(status_code=400, detail="dish_name no puede estar vacío")
    lines = [{"ingredient_id": l.ingredient_id, "quantity": l.quantity} for l in body.lines]
    result = await db.db_upsert_dish_recipe(restaurant["id"], body.dish_name, lines)
    return {"success": True, "dish_name": body.dish_name, "lines": result}


@router.delete("/api/inventory/recipes/{dish_name}")
async def delete_recipe(
    dish_name: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Deletes all ingredients from a dish's recipe."""
    await db.db_delete_dish_recipe(restaurant["id"], dish_name)
    return {"success": True}


@router.get("/api/inventory/food-costs")
async def get_food_costs(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Food cost of each dish with a per-ingredient breakdown."""
    costs = await db.db_get_food_costs(restaurant["id"])
    return {"food_costs": costs}
