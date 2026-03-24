from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from app.services import database as db
from app.routes.deps import require_auth, get_current_restaurant

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
async def get_inventory(request: Request):
    """Lista todos los productos del inventario"""
    restaurant = await get_current_restaurant(request)
    items = await db.db_get_inventory(restaurant["id"])
    return {"items": items}


@router.post("/api/inventory")
async def create_inventory_item(request: Request, body: InventoryItemCreate):
    """Crea un nuevo producto en el inventario"""
    restaurant = await get_current_restaurant(request)
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
async def update_inventory_item(request: Request, item_id: int, body: InventoryItemUpdate):
    """Actualiza un producto del inventario"""
    await require_auth(request)
    item = await db.db_update_inventory_item(item_id, body.dict(exclude_none=True))
    if not item:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return {"success": True, "item": item}


@router.delete("/api/inventory/{item_id}")
async def delete_inventory_item(request: Request, item_id: int):
    """Elimina un producto del inventario"""
    await require_auth(request)
    await db.db_delete_inventory_item(item_id)
    return {"success": True}


@router.post("/api/inventory/{item_id}/adjust")
async def adjust_stock(request: Request, item_id: int, body: StockAdjustment):
    """Ajusta el stock manualmente (reposición, merma, etc.)"""
    restaurant = await get_current_restaurant(request)
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
async def get_stock_history(request: Request, item_id: int):
    """Historial de movimientos de stock"""
    await require_auth(request)
    history = await db.db_get_inventory_history(item_id)
    return {"history": history}


@router.get("/api/inventory/alerts")
async def get_inventory_alerts(request: Request):
    """Productos con stock bajo o agotado"""
    restaurant = await get_current_restaurant(request)
    alerts = await db.db_get_inventory_alerts(restaurant["id"])
    return {"alerts": alerts}


@router.get("/api/inventory/menu-items")
async def get_menu_items_for_linking(request: Request):
    """Devuelve todos los platos del menú para el selector de vinculación"""
    restaurant = await get_current_restaurant(request)
    menu = await db.db_get_menu(restaurant["whatsapp_number"]) or {}
    dishes = []
    for category, items in menu.items():
        for item in items:
            dishes.append({"name": item.get("name", ""), "category": category})
    return {"dishes": dishes}