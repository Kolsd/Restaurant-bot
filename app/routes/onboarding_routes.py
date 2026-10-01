"""
app/routes/onboarding_routes.py

GET /api/onboarding — what this restaurant still has to do, for the
restaurant itself.

Distinct from `/api/internal/admin/organizations/{id}/onboarding`, which is
the Mesio team's view of where an account is stuck, lives in the internal
namespace, and reads across tenants. This one is an app feature: scoped to
the caller's own org, and phrased as next steps rather than as stages.

Read-only. It never fails the page: `services.onboarding.get_checklist`
turns an unreadable count into "not done yet".
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.routes.deps import get_current_restaurant, get_current_user, require_auth, resolve_sede_filter
from app.services.logging import get_logger
from app.services.onboarding import get_checklist
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter()


@router.get("/api/onboarding")
async def get_onboarding(request: Request):
    """The setup checklist for the caller's own restaurant and sede.

    Any signed-in member of the restaurant may read it — it is guidance,
    not data, and a gerente setting up their sede needs the same list the
    owner sees. The sede comes from `resolve_sede_filter`, so the counts
    that belong to a sede (its tables) are that sede's, exactly like every
    other staff-facing listing.
    """
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    location_id = resolve_sede_filter(request, user)

    with tenant_scope(org_id):
        checklist = await get_checklist(org_id, location_id)

    return checklist
