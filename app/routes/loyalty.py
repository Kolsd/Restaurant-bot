"""
app/routes/loyalty.py
=====================
REST endpoints for the Loyalty module.

All endpoints require:
  - A valid Bearer token   (via get_current_restaurant)
  - The 'loyalty' module active on the restaurant (via require_module)

Designed for minimal token usage:
  GET /api/loyalty/balance   → {"puntos_actuales": N, "equivalencia_cop": N*val}
  The bot consumes this endpoint as a tool — response is < 60 bytes of JSON.

Aggregate endpoints (dashboard):
  GET /api/loyalty/aggregates  → headline KPIs
  GET /api/loyalty/segments    → 4 IA segments with counts and potential revenue
  GET /api/loyalty/funnel      → 5-step conversion funnel

Campaign CRUD:
  GET    /api/loyalty/campaigns
  POST   /api/loyalty/campaigns
  PATCH  /api/loyalty/campaigns/{campaign_id}
  DELETE /api/loyalty/campaigns/{campaign_id}
  POST   /api/loyalty/campaigns/{campaign_id}/toggle
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import database as db
from app.repositories import loyalty_repo
from app.repositories import loyalty_campaigns_repo
from app.routes.deps import get_current_restaurant_scoped, require_module
from app.services.logging import get_logger

log = get_logger(__name__)

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


class CampaignCreate(BaseModel):
    name:             str           = Field(..., min_length=1, max_length=200)
    description:      str           = Field(default="", max_length=2000)
    segment:          Optional[str] = Field(default=None, max_length=100)
    trigger_type:     str           = Field(..., min_length=1, max_length=50)
    message_template: str           = Field(..., min_length=1, max_length=4096)


class CampaignUpdate(BaseModel):
    name:             Optional[str] = Field(default=None, min_length=1, max_length=200)
    description:      Optional[str] = Field(default=None, max_length=2000)
    segment:          Optional[str] = Field(default=None, max_length=100)
    trigger_type:     Optional[str] = Field(default=None, min_length=1, max_length=50)
    message_template: Optional[str] = Field(default=None, min_length=1, max_length=4096)


class ToggleBody(BaseModel):
    status: str = Field(..., description="'active' or 'paused'")


# ── Endpoints ─────────────────────────────────────────────────────────

@router.get("/balance", dependencies=[_module_dep])
async def get_loyalty_balance(
    phone:      str  = Query(..., min_length=7, max_length=60),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    Ultra-lightweight tool for the bot and the POS.
    O(1) response from loyalty_customers (no joins, no history).
    Returns 404 if the customer doesn't have a loyalty record yet.
    """
    # Synthetic diner web-chat identities ("web:<uuid4>", see diner_sessions_repo)
    # are NOT phone numbers. Reject them explicitly instead of letting the
    # digit-stripping below silently mangle a UUID's hex digits into a
    # garbage "phone" that could coincidentally collide with a real customer.
    # max_length is 60 (not 15) so this check is actually reachable — a
    # "web:<uuid4>" token is ~40 chars and would otherwise be rejected by
    # Pydantic's own length validation first, with a generic error shape
    # instead of this explicit, clear one.
    if phone.startswith("web:"):
        raise HTTPException(status_code=422, detail="Número de teléfono inválido")
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
    phone:      str  = Query(..., min_length=7, max_length=60),
    limit:      int  = Query(default=50, ge=1, le=200),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """History of a customer's movements (for dashboard / POS)."""
    # See get_loyalty_balance above — web:<uuid4> diner identities are not phones.
    # max_length=60 (not 15) so this explicit check is reachable for a ~40-char
    # "web:<uuid4>" token instead of being shadowed by Pydantic's own length gate.
    if phone.startswith("web:"):
        raise HTTPException(status_code=422, detail="Número de teléfono inválido")
    clean = "".join(c for c in phone if c.isdigit())
    if len(clean) < 7:
        raise HTTPException(status_code=422, detail="Número de teléfono inválido")

    entries = await db.db_get_loyalty_ledger(restaurant["id"], clean, limit)
    return {"phone": clean, "entries": entries, "total": len(entries)}


@router.get("/stats", dependencies=[_module_dep])
async def get_loyalty_stats(
    limit:      int  = Query(default=100, ge=1, le=500),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Top customers by points balance (for the loyalty dashboard)."""
    rows = await db.db_get_loyalty_stats(restaurant["id"], limit)
    return {"customers": rows, "total": len(rows)}


@router.post("/redeem", dependencies=[_module_dep])
async def redeem_loyalty_points(
    body:       RedeemBody,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    Redeems points at the POS at payment time.
    Returns the discount in COP and the new balance.
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


@router.get("/customers", dependencies=[_module_dep])
async def list_loyalty_customers(
    limit:      int  = Query(default=50, ge=1, le=200),
    offset:     int  = Query(default=0, ge=0),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    List loyalty members ordered by most recent activity.
    Returns paginated customer list with points and activity data.
    """
    org_id = restaurant["id"]
    customers = await loyalty_repo.db_get_loyalty_customers(org_id, limit=limit, offset=offset)
    total = await loyalty_repo.db_count_loyalty_customers(org_id)
    return {"count": total, "customers": customers}


@router.post("/adjust", dependencies=[_module_dep])
async def adjust_loyalty_points(
    body:       AdjustBody,
    restaurant: dict = Depends(get_current_restaurant_scoped),
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


# ── Aggregate endpoints (PART A) ─────────────────────────────────────────────

@router.get("/aggregates", dependencies=[_module_dep])
async def get_loyalty_aggregates(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    Headline KPIs for the loyalty dashboard.

    Returns: total_customers, avg_frequency, avg_ticket_member,
             avg_ticket_nonmember, roi_multiple (null), redemption_pct.
    """
    return await loyalty_repo.db_get_loyalty_aggregates(restaurant["id"])


@router.get("/segments", dependencies=[_module_dep])
async def get_loyalty_segments(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    4 IA-curated customer segments with counts and potential revenue estimates.

    Segments: vip-active, vip-dormant, new-month, birthdays.
    """
    segments = await loyalty_repo.db_get_loyalty_segments(restaurant["id"])
    return {"segments": segments}


@router.get("/funnel", dependencies=[_module_dep])
async def get_loyalty_funnel(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """
    5-step conversion funnel for the loyalty program.

    Steps: primera visita → segunda visita → plata → oro → platino.
    Percentages are relative to step 1.
    """
    steps = await loyalty_repo.db_get_loyalty_funnel(restaurant["id"])
    return {"steps": steps}


# ── Campaigns CRUD (PART B) ───────────────────────────────────────────────────

@router.get("/campaigns", dependencies=[_module_dep])
async def list_campaigns(
    status:     Optional[str] = Query(
        default=None, description="Filter by status: draft|active|paused"
    ),
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """List campaigns for this restaurant, optionally filtered by status."""
    if status and status not in ("draft", "active", "paused"):
        raise HTTPException(
            status_code=422,
            detail="status must be one of: draft, active, paused",
        )
    campaigns = await loyalty_campaigns_repo.db_list_campaigns(status=status)
    return {"campaigns": campaigns, "total": len(campaigns)}


@router.post("/campaigns", dependencies=[_module_dep], status_code=201)
async def create_campaign(
    body:       CampaignCreate,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Create a new campaign in 'draft' status."""
    campaign = await loyalty_campaigns_repo.db_create_campaign(
        org_id=restaurant["id"],
        name=body.name,
        description=body.description,
        segment=body.segment,
        trigger_type=body.trigger_type,
        message_template=body.message_template,
    )
    return campaign


@router.patch("/campaigns/{campaign_id}", dependencies=[_module_dep])
async def update_campaign(
    campaign_id: str,
    body:        CampaignUpdate,
    restaurant:  dict = Depends(get_current_restaurant_scoped),
):
    """Partially update a campaign's editable fields.

    Only name, description, segment, trigger_type, message_template are patchable.
    """
    updates = body.model_dump(exclude_none=True)
    result = await loyalty_campaigns_repo.db_update_campaign(campaign_id, updates)
    if result is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return result


@router.delete("/campaigns/{campaign_id}", dependencies=[_module_dep], status_code=204)
async def delete_campaign(
    campaign_id: str,
    restaurant:  dict = Depends(get_current_restaurant_scoped),
):
    """Delete a campaign.  Returns 204 on success, 404 if not found."""
    deleted = await loyalty_campaigns_repo.db_delete_campaign(campaign_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Campaign not found")
    # 204 No Content — FastAPI sends no body


@router.post("/campaigns/{campaign_id}/toggle", dependencies=[_module_dep])
async def toggle_campaign(
    campaign_id: str,
    body:        ToggleBody,
    restaurant:  dict = Depends(get_current_restaurant_scoped),
):
    """
    Transition campaign status following the state machine:
      draft   → active   (launch)
      active  → paused   (pause)
      paused  → active   (resume)

    Returns 409 for invalid transitions.
    Returns 404 if the campaign is not found.
    """
    if body.status not in ("active", "paused"):
        raise HTTPException(
            status_code=422,
            detail="status must be 'active' or 'paused'",
        )
    try:
        result = await loyalty_campaigns_repo.db_toggle_campaign_status(
            campaign_id=campaign_id,
            new_status=body.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if result is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return result
