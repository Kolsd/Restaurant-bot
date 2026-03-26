"""
app/routes/loyalty.py
=====================
Endpoints REST del módulo de Fidelización (Loyalty).

Todos los endpoints requieren:
  - Bearer token válido   (via get_current_restaurant)
  - Módulo 'loyalty' activo en el restaurante (via require_module)

Diseño orientado a mínimos tokens:
  GET /api/loyalty/balance   → {"puntos_actuales": N, "equivalencia_cop": N*val}
  El bot consume este endpoint como herramienta — respuesta de < 60 bytes JSON.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from app.services import database as db
from app.routes.deps import get_current_restaurant, require_module

router = APIRouter(prefix="/api/loyalty", tags=["loyalty"])

_MODULE = "loyalty"
_module_dep = Depends(require_module(_MODULE))


# ── Modelos de entrada ────────────────────────────────────────────────

class RedeemBody(BaseModel):
    phone:    str = Field(..., min_length=7, max_length=15)
    points:   int = Field(..., gt=0)
    order_id: str = Field(..., min_length=1, max_length=100)


class AdjustBody(BaseModel):
    phone:  str = Field(..., min_length=7, max_length=15)
    delta:  int = Field(..., description="Positivo = sumar, negativo = restar")
    reason: str = Field(default="manual_adjust", max_length=100)


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/balance", dependencies=[_module_dep])
async def get_loyalty_balance(
    phone:      str  = Query(..., min_length=7, max_length=15),
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Herramienta ultra-ligera para el bot y el POS.
    Respuesta O(1) desde loyalty_customers (sin joins, sin historial).
    Retorna 404 si el cliente aún no tiene registro de fidelización.
    """
    clean = "".join(c for c in phone if c.isdigit())
    if len(clean) < 7:
        raise HTTPException(status_code=422, detail="Número de teléfono inválido")

    balance = await db.db_get_loyalty_balance(restaurant["id"], clean)
    if balance is None:
        raise HTTPException(
            status_code=404,
            detail="El cliente no tiene registro de fidelización",
        )
    return balance


@router.get("/ledger", dependencies=[_module_dep])
async def get_loyalty_ledger(
    phone:      str  = Query(..., min_length=7, max_length=15),
    limit:      int  = Query(default=50, ge=1, le=200),
    restaurant: dict = Depends(get_current_restaurant),
):
    """Historial de movimientos de un cliente (para dashboard / POS)."""
    clean = "".join(c for c in phone if c.isdigit())
    if len(clean) < 7:
        raise HTTPException(status_code=422, detail="Número de teléfono inválido")

    entries = await db.db_get_loyalty_ledger(restaurant["id"], clean, limit)
    return {"phone": clean, "entries": entries, "total": len(entries)}


@router.get("/stats", dependencies=[_module_dep])
async def get_loyalty_stats(
    limit:      int  = Query(default=100, ge=1, le=500),
    restaurant: dict = Depends(get_current_restaurant),
):
    """Top clientes por saldo de puntos (para dashboard de fidelización)."""
    rows = await db.db_get_loyalty_stats(restaurant["id"], limit)
    return {"customers": rows, "total": len(rows)}


@router.post("/redeem", dependencies=[_module_dep])
async def redeem_loyalty_points(
    body:       RedeemBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Canjea puntos en el POS al momento del pago.
    Retorna el descuento en COP y el nuevo saldo.
    """
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
async def adjust_loyalty_points(
    body:       AdjustBody,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Ajuste manual de puntos (admin/soporte).
    delta positivo = sumar, negativo = restar. No deja saldo negativo.
    """
    try:
        result = await db.db_adjust_loyalty_points(
            restaurant_id=restaurant["id"],
            phone=body.phone,
            delta=body.delta,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result
