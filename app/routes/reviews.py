"""
Reviews API — public review management built on top of NPS responses.

Endpoints:
  GET  /api/reviews         — list public reviews for the dashboard
  GET  /api/reviews/summary — aggregate stats (avg rating, distribution)
  PUT  /api/reviews/{id}/publish — toggle public visibility + customer name
  PUT  /api/reviews/{id}/reply   — save owner reply
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.routes.deps import require_auth, get_current_restaurant_scoped
from app.repositories import reviews_repo as rr
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/api/reviews",
    tags=["reviews"],
    dependencies=[Depends(require_auth)],
)


class PublishBody(BaseModel):
    is_public: bool = True
    customer_name: str = ""


class ReplyBody(BaseModel):
    reply: str = Field(..., min_length=1, max_length=2000)


@router.get("")
async def list_reviews(
    limit: int = 50,
    restaurant=Depends(get_current_restaurant_scoped),
):
    """Return public reviews for the authenticated restaurant's org.

    Wave-2: uses org-scoped query so matriz admins see reviews from all sedes.
    RLS (tenant_scope set by get_current_restaurant_scoped) enforces cross-tenant isolation.
    """
    reviews = await rr.db_get_public_reviews_by_org(limit=limit)
    return {"reviews": reviews}


@router.get("/summary")
async def get_review_summary(restaurant=Depends(get_current_restaurant_scoped)):
    """Return aggregate review stats for the full org (all locations)."""
    summary = await rr.db_get_review_summary_by_org()
    return summary


async def _verify_review_ownership(nps_id: int) -> None:
    """Verify the NPS response exists within the current tenant scope.

    Wave-2: RLS ensures the nps_responses row is only visible if it belongs
    to the caller's org — no bot_number filter needed.
    """
    exists = await rr.db_verify_review_ownership_by_org(nps_id)
    if not exists:
        raise HTTPException(status_code=404, detail="Review not found")


@router.put("/{nps_id}/publish")
async def publish_review(
    nps_id: int,
    body: PublishBody,
    restaurant=Depends(get_current_restaurant_scoped),
):
    """Toggle public visibility for an NPS response and set display name."""
    await _verify_review_ownership(nps_id)
    updated = await rr.db_set_review_public(
        nps_id,
        is_public=body.is_public,
        customer_name=body.customer_name,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"success": True, "review": updated}


@router.put("/{nps_id}/reply")
async def reply_to_review(
    nps_id: int,
    body: ReplyBody,
    restaurant=Depends(get_current_restaurant_scoped),
):
    """Save owner reply to a review."""
    await _verify_review_ownership(nps_id)
    updated = await rr.db_add_owner_reply(nps_id, body.reply.strip())
    if not updated:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"success": True, "review": updated}
