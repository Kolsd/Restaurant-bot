"""
Discounts router — CRUD for dynamic time-slot discounts (yield management).

All endpoints require:
  - valid admin JWT (require_auth)
  - the "dynamic_discounts" feature flag enabled (require_module)
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.routes.deps import require_auth, get_current_restaurant, require_module
from app.repositories import discounts_repo as dr
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/discounts",
    tags=["discounts"],
    dependencies=[Depends(require_auth), Depends(require_module("dynamic_discounts"))],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class DiscountCreate(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6, description="0=Mon … 6=Sun")
    start_time: str = Field(..., description="HH:MM or HH:MM:SS")
    end_time: str = Field(..., description="HH:MM or HH:MM:SS")
    discount_percent: int = Field(..., ge=5, le=50)
    branch_id: int | None = None
    label: str = ""


class DiscountUpdate(BaseModel):
    day_of_week: int | None = Field(None, ge=0, le=6)
    start_time: str | None = None
    end_time: str | None = None
    discount_percent: int | None = Field(None, ge=5, le=50)
    label: str | None = None
    active: bool | None = None
    branch_id: int | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

# NOTE: /active must be declared BEFORE /{id} to avoid FastAPI treating
#       "active" as an integer path parameter.

@router.get("/active")
async def get_active_discount(
    restaurant=Depends(get_current_restaurant),
):
    """Return the currently active discount for this restaurant (right now), or null."""
    discount = await dr.db_get_active_discount(restaurant["id"])
    return JSONResponse({"discount": discount})


@router.get("")
async def list_discounts(
    restaurant=Depends(get_current_restaurant),
):
    """List all time-slot discounts for this restaurant."""
    discounts = await dr.db_get_all_discounts(restaurant["id"])
    return JSONResponse({"discounts": discounts})


@router.post("")
async def create_discount(
    body: DiscountCreate,
    restaurant=Depends(get_current_restaurant),
):
    """Create (or upsert on conflict) a time-slot discount."""
    try:
        discount = await dr.db_create_discount(
            restaurant_id=restaurant["id"],
            day_of_week=body.day_of_week,
            start_time=body.start_time,
            end_time=body.end_time,
            discount_percent=body.discount_percent,
            branch_id=body.branch_id,
            label=body.label,
        )
        return JSONResponse({"discount": discount}, status_code=201)
    except Exception:
        log.exception("discounts.create.error", restaurant_id=restaurant["id"])
        raise HTTPException(status_code=500, detail="Error al crear descuento")


async def _verify_discount_ownership(discount_id: int, restaurant: dict) -> dict:
    """Fetch all discounts for restaurant and verify this discount_id belongs to it."""
    all_discounts = await dr.db_get_all_discounts(restaurant["id"])
    match = next((d for d in all_discounts if d.get("id") == discount_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Discount not found")
    return match


@router.put("/{discount_id}")
async def update_discount(
    discount_id: int,
    body: DiscountUpdate,
    restaurant=Depends(get_current_restaurant),
):
    """Update allowed fields on a discount."""
    await _verify_discount_ownership(discount_id, restaurant)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updated = await dr.db_update_discount(discount_id, **updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Discount not found")
    return JSONResponse({"discount": updated})


@router.delete("/{discount_id}")
async def delete_discount(
    discount_id: int,
    restaurant=Depends(get_current_restaurant),
):
    """Delete a discount by id."""
    await _verify_discount_ownership(discount_id, restaurant)
    deleted = await dr.db_delete_discount(discount_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Discount not found")
    return JSONResponse({"ok": True})
