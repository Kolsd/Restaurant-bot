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
    """Save owner reply to a review and best-effort deliver it via WhatsApp.

    The reply is always persisted. WhatsApp delivery is attempted when we
    have phone + bot_number + WA token; failures are logged but never block
    the API response (the dashboard already shows the saved reply regardless).
    Successful delivery flips owner_reply_delivered_at via
    db_mark_review_reply_delivered.
    """
    await _verify_review_ownership(nps_id)
    updated = await rr.db_add_owner_reply(nps_id, body.reply.strip())
    if not updated:
        raise HTTPException(status_code=404, detail="Review not found")

    delivered = False
    try:
        review = await rr.db_get_review(nps_id)
        if review and review.get("phone") and review.get("bot_number"):
            from app.services import database as db
            # Ownership was verified above; the bot_number belongs to the
            # caller's org so the lookup stays within scope. No bypass needed.
            rest = await db.db_get_restaurant_by_phone(review["bot_number"])
            token    = (rest or {}).get("wa_access_token", "") or ""
            phone_id = (rest or {}).get("wa_phone_id", "") or ""
            if token:
                from app.services.meta_api import send_text
                resto_name = (rest or {}).get("name", "") or "El equipo"
                msg = (
                    f"Hola, gracias por tu reseña. Queríamos contarte:\n\n"
                    f"{body.reply.strip()}\n\n— {resto_name}"
                )
                ok = await send_text(
                    review["bot_number"], token, review["phone"], msg,
                    phone_id=phone_id,
                )
                if ok:
                    await rr.db_mark_review_reply_delivered(nps_id)
                    delivered = True
    except Exception:
        log.exception("review.reply_delivery_failed", review_id=nps_id)

    return {"success": True, "review": updated, "delivered": delivered}
