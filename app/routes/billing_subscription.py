"""app/routes/billing_subscription.py

Subscription plan management endpoints for restaurant admin UI.

All authenticated endpoints use get_current_restaurant_scoped which activates
tenant_scope(org_id) for the duration of the request, so plan_limits_repo
calls are automatically RLS-scoped.

Endpoints:
  GET  /api/billing/plan          — current plan + addons + usage
  GET  /api/billing/usage         — detailed usage with cap status per dimension
  GET  /api/billing/plans         — public list of all plans + addons (no auth)

Conversation packs and auto-recharge were removed 2026-09-29: Mesio sells a
flat price per sede and the conversation allowance is an internal soft
ceiling (app/services/plan_enforcement.py).
"""

from fastapi import APIRouter, Depends, HTTPException

from app.routes.deps import get_current_restaurant_scoped
from app.repositories import plan_limits_repo
from app.services import database as db
from app.services import plans
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing-subscription"])


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/plan")
async def get_current_plan(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return the restaurant's current plan, addons and period usage.

    Auth: restaurant admin Bearer token (get_current_restaurant_scoped activates
    tenant_scope so plan_limits_repo reads are RLS-scoped).
    """
    org_id: int = restaurant["id"]
    try:
        sub = await plan_limits_repo.db_get_org_subscription(org_id)
    except Exception as exc:
        log.exception("billing_subscription.get_plan_error", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al cargar información del plan") from exc

    # Pricing is per sede (docs/claude/status.md #15): what the owner pays is
    # the plan's price — or their frozen founder price — times active sedes.
    sedes = max(1, len(await db.db_get_org_locations(org_id, active_only=True)))
    plan_code = plans.normalize_plan(sub.get("plan_code"))
    founder_price_cop = sub.get("founder_price_cop")
    price_per_sede = plans.monthly_price_per_sede(plan_code, founder_price_cop)
    comp_until = sub.get("comp_until")

    # JSON boundary: Decimal audio_min_used already converted by _row_to_dict
    return {
        "plan_code":      plan_code,
        "plan_name":      plans.PLAN_NAMES[plan_code],
        "effective_plan": plans.effective_plan(plan_code, comp_until),
        "monthly_price_cop": price_per_sede,  # integer COP, per sede
        "list_price_cop": plans.PRICES_COP[plan_code],
        "founder":        founder_price_cop is not None,
        "sedes":          sedes,
        "monthly_total_cop": price_per_sede * sedes,
        "in_trial":       plans.in_trial(comp_until),
        "active_addons":  sub.get("active_addons") or [],
        "annual_billing": sub.get("annual_billing", False),
        "current_period": {
            "start":          sub.get("current_period_start"),
            "convs_used":     sub.get("current_period_convs_used", 0),
            "audio_min_used": sub.get("current_period_audio_min_used", 0.0),
        },
        "caps": {
            "conv_cap":       sub.get("conv_cap"),
            "audio_min_cap":  sub.get("audio_min_cap"),
            "storage_mb_cap": sub.get("storage_mb_cap"),
            "staff_cap":      sub.get("staff_cap"),
            "sku_cap":        sub.get("sku_cap"),
            "marketing_msg_cap": sub.get("marketing_msg_cap"),
            "locations_included": sub.get("locations_included"),
        },
        "comp_until": sub.get("comp_until"),
    }


@router.get("/usage")
async def get_usage(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return detailed usage with cap status per dimension.

    Status values per dimension: ok, warn50, warn80, warn90, exceeded, comp.
    comp means the org has an active comp_until override (friends & family).
    """
    org_id: int = restaurant["id"]
    try:
        caps = await plan_limits_repo.db_check_caps(org_id)
    except Exception as exc:
        log.exception("billing_subscription.get_usage_error", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al cargar usage") from exc

    return caps


@router.get("/plans")
async def list_plans():
    """Return all available plans and addon modules. No auth required.

    Consumed by the landing page and pricing UI to display plan options.
    """
    try:
        plans = await plan_limits_repo.db_list_plans()
        addons = await plan_limits_repo.db_list_addons()
    except Exception as exc:
        log.exception("billing_subscription.list_plans_error")
        raise HTTPException(status_code=500, detail="Error al cargar planes") from exc

    return {
        "plans":  plans,
        "addons": addons,
    }
