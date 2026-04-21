from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from app.services import database as db
from app.routes.deps import require_auth, get_current_restaurant_scoped
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter()


class InventoryItemCreate(BaseModel):
    name: str
    unit: str = "unidades"          # unidades, kg, litros, etc.
    current_stock: float
    min_stock: float = 0            # umbral de alerta
    linked_dishes: List[str] = []   # nombres exactos de platos del menú
    cost_per_unit: float = 0        # costo por unidad (opcional)


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
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Lista todos los productos del inventario"""
    items = await db.db_get_inventory(restaurant["id"])
    return {"items": items}


@router.post("/api/inventory")
async def create_inventory_item(
    body: InventoryItemCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Crea un nuevo producto en el inventario"""
    item = await db.db_create_inventory_item(
        restaurant_id=restaurant["id"],
        name=body.name,
        unit=body.unit,
        current_stock=body.current_stock,
        min_stock=body.min_stock,
        linked_dishes=body.linked_dishes,
        cost_per_unit=body.cost_per_unit
    )
    return {"success": True, "item": item}


@router.put("/api/inventory/{item_id}")
async def update_inventory_item(
    request: Request,
    item_id: int,
    body: InventoryItemUpdate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Actualiza un producto del inventario"""
    existing = await db.db_get_inventory_item(item_id)
    # Wave-2: inventory rows carry org_id (restaurant_id dropped in 0038).
    if not existing or existing.get("org_id") != restaurant["id"]:
        log.warning("inventory.update_idor_attempt", item_id=item_id, org_id=restaurant["id"])
        raise HTTPException(status_code=404, detail="Producto no encontrado")
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
    """Elimina un producto del inventario"""
    existing = await db.db_get_inventory_item(item_id)
    # Wave-2: inventory rows carry org_id (restaurant_id dropped in 0038).
    if not existing or existing.get("org_id") != restaurant["id"]:
        log.warning("inventory.delete_idor_attempt", item_id=item_id, org_id=restaurant["id"])
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    await db.db_delete_inventory_item(item_id)
    return {"success": True}


@router.post("/api/inventory/{item_id}/adjust")
async def adjust_stock(
    item_id: int,
    body: StockAdjustment,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Ajusta el stock manualmente (reposición, merma, etc.)"""
    result = await db.db_adjust_inventory_stock(
        item_id=item_id,
        quantity_delta=body.quantity,
        reason=body.reason,
        restaurant_id=restaurant["id"]
    )
    if not result:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return {"success": True, "item": result}


@router.get("/api/inventory/{item_id}/history")
async def get_stock_history(
    request: Request,
    item_id: int,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Historial de movimientos de stock"""
    existing = await db.db_get_inventory_item(item_id)
    # Wave-2: inventory rows carry org_id (restaurant_id dropped in 0038).
    if not existing or existing.get("org_id") != restaurant["id"]:
        log.warning("inventory.history_idor_attempt", item_id=item_id, org_id=restaurant["id"])
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    history = await db.db_get_inventory_history(item_id)
    return {"history": history}


@router.get("/api/inventory/alerts")
async def get_inventory_alerts(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Productos con stock bajo o agotado"""
    alerts = await db.db_get_inventory_alerts(restaurant["id"])
    return {"alerts": alerts}


@router.get("/api/inventory/menu-items")
async def get_menu_items_for_linking(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Devuelve todos los platos del menú para el selector de vinculación"""
    menu = await db.db_get_menu(restaurant["whatsapp_number"]) or {}
    dishes = []
    for category, items in menu.items():
        for item in items:
            dishes.append({"name": item.get("name", ""), "category": category})
    return {"dishes": dishes}


# ── ESCANDALLOS / RECETAS (FASE 4) ────────────────────────────────────────────

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
    """Lista todos los escandallos con food cost por plato."""
    recipes = await db.db_get_all_recipes(restaurant["id"])
    return {"recipes": recipes}


@router.get("/api/inventory/recipes/{dish_name}")
async def get_recipe(
    dish_name: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Devuelve las líneas de ingredientes de un plato."""
    lines = await db.db_get_dish_recipe(restaurant["id"], dish_name)
    return {"dish_name": dish_name, "lines": lines}


@router.post("/api/inventory/recipes")
async def upsert_recipe(
    body: RecipeUpsert,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Crea o reemplaza el escandallo completo de un plato."""
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
    """Elimina todos los ingredientes del escandallo de un plato."""
    await db.db_delete_dish_recipe(restaurant["id"], dish_name)
    return {"success": True}


@router.get("/api/inventory/food-costs")
async def get_food_costs(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Food cost de cada plato con desglose por ingrediente."""
    costs = await db.db_get_food_costs(restaurant["id"])
    return {"food_costs": costs}
