"""
app/routes/loyalty.py
=====================
Endpoints REST del módulo de Fidelización (Loyalty).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from app.services import database as db
from app.routes.deps import get_current_restaurant, require_module, get_current_user

router = APIRouter(prefix="/api/loyalty", tags=["loyalty"])

_MODULE = "loyalty"
_module_dep = Depends(require_module(_MODULE))

def _resolve_branch_id(request: Request, user: dict, restaurant: dict) -> int | str | None:
    branch_header = request.headers.get("X-Branch-ID")
    is_admin = any(r in user.get("role", "") for r in ["owner", "admin"])
    
    if is_admin:
        if branch_header == "all": return "all"
        elif branch_header == "matriz": return None
        elif branch_header and branch_header.isdigit(): return int(branch_header)
        return None
        
    return user.get("branch_id") or (restaurant["id"] if restaurant.get("parent_restaurant_id") else None)
    
class RedeemBody(BaseModel):
    phone:    str = Field(..., min_length=7, max_length=15)
    points:   int = Field(..., gt=0)
    order_id: str = Field(..., min_length=1, max_length=100)

class AdjustBody(BaseModel):
    phone:  str = Field(..., min_length=7, max_length=15)
    delta:  int = Field(...)
    reason: str = Field(..., max_length=100)

@router.get("/balance", dependencies=[_module_dep])
async def get_loyalty_balance(
    request: Request,
    phone: str = Query(..., min_length=7, max_length=15),
):
    restaurant = await get_current_restaurant(request)
    matriz_id = restaurant.get("parent_restaurant_id") or restaurant["id"]
    result = await db.db_get_loyalty_balance(matriz_id, phone)
    return result

@router.get("/stats", dependencies=[_module_dep])
async def get_loyalty_stats(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
):
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    # 🛡️ Leer la sucursal del Dashboard
    branch_id = _resolve_branch_id(request, user, restaurant)
    
    rows = await db.db_get_loyalty_stats(restaurant["id"], limit, branch_id=branch_id)
    return {"customers": rows, "total": len(rows)}

@router.post("/redeem", dependencies=[_module_dep])
async def redeem_loyalty_points(request: Request, body: RedeemBody):
    restaurant = await get_current_restaurant(request)
    try:
        result = await db.db_redeem_loyalty_points(
            restaurant_id=restaurant["id"],
            phone=body.phone,
            points=body.points,
            order_id=body.order_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result

@router.post("/adjust", dependencies=[_module_dep])
async def adjust_loyalty_points(request: Request, body: AdjustBody):
    restaurant = await get_current_restaurant(request)
    try:
        result = await db.db_adjust_loyalty_points(
            restaurant_id=restaurant["id"],
            phone=body.phone,
            delta=body.delta,
            reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "new_balance": result}