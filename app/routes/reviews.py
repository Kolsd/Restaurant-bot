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

from app.routes.deps import require_auth, get_current_restaurant
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
    restaurant=Depends(get_current_restaurant),
):
    """Return public reviews for the authenticated restaurant."""
    bot_number = restaurant["whatsapp_number"]
    reviews = await rr.db_get_public_reviews(bot_number, limit=limit)
    return {"reviews": reviews}


@router.get("/summary")
async def get_review_summary(restaurant=Depends(get_current_restaurant)):
    """Return aggregate review stats: total, avg score, distribution by score."""
    bot_number = restaurant["whatsapp_number"]
    summary = await rr.db_get_review_summary(bot_number)
    return summary


async def _verify_review_ownership(nps_id: int, bot_number: str) -> None:
    """Verify the NPS response belongs to this restaurant's bot_number."""
    exists = await rr.db_verify_review_ownership(nps_id, bot_number)
    if not exists:
        raise HTTPException(status_code=404, detail="Review not found")


@router.put("/{nps_id}/publish")
async def publish_review(
    nps_id: int,
    body: PublishBody,
    restaurant=Depends(get_current_restaurant),
):
    """Toggle public visibility for an NPS response and set display name."""
    bot_number = restaurant["whatsapp_number"]
    await _verify_review_ownership(nps_id, bot_number)
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
    restaurant=Depends(get_current_restaurant),
):
    """Save owner reply to a review."""
    bot_number = restaurant["whatsapp_number"]
    await _verify_review_ownership(nps_id, bot_number)
    updated = await rr.db_add_owner_reply(nps_id, body.reply.strip())
    if not updated:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"success": True, "review": updated}
