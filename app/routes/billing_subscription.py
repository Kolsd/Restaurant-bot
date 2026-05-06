"""app/routes/billing_subscription.py

Subscription plan management endpoints for restaurant admin UI.

All authenticated endpoints use get_current_restaurant_scoped which activates
tenant_scope(org_id) for the duration of the request, so plan_limits_repo
calls are automatically RLS-scoped.

Endpoints:
  GET  /api/billing/plan          — current plan + addons + auto-recharge + usage
  GET  /api/billing/usage         — detailed usage with cap status per dimension
  POST /api/billing/auto-recharge — update auto-recharge settings
  POST /api/billing/buy-pack      — manual pack purchase (Wompi stub)
  GET  /api/billing/plans         — public list of all plans + addons (no auth)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.routes.deps import get_current_restaurant_scoped
from app.repositories import plan_limits_repo
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing-subscription"])


# ── Pydantic models ───────────────────────────────────────────────────────────


class AutoRechargePayload(BaseModel):
    enabled: bool
    max_packs: int = 5


class BuyPackPayload(BaseModel):
    # Future: will carry Wompi payment token when wired.
    # For now, just an optional note so the endpoint has a body schema.
    note: str | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/plan")
async def get_current_plan(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return the restaurant's current plan, addons, auto-recharge config, and period usage.

    Auth: restaurant admin Bearer token (get_current_restaurant_scoped activates
    tenant_scope so plan_limits_repo reads are RLS-scoped).
    """
    org_id: int = restaurant["id"]
    try:
        sub = await plan_limits_repo.db_get_org_subscription(org_id)
    except Exception as exc:
        log.exception("billing_subscription.get_plan_error", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al cargar información del plan") from exc

    # JSON boundary: Decimal audio_min_used already converted by _row_to_dict
    return {
        "plan_code":      sub.get("plan_code"),
        "plan_name":      sub.get("plan_display_name"),
        "monthly_price_cop": sub.get("monthly_price_cop"),  # integer COP
        "active_addons":  sub.get("active_addons") or [],
        "annual_billing": sub.get("annual_billing", False),
        "auto_recharge": {
            "enabled":       sub.get("auto_recharge_enabled", False),
            "max_packs":     sub.get("auto_recharge_max_packs_per_month", 5),
        },
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


@router.post("/auto-recharge")
async def set_auto_recharge(
    payload: AutoRechargePayload,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Update auto-recharge configuration.

    max_packs must be between 0 and 5 (hard cap).
    Returns the updated config.
    """
    org_id: int = restaurant["id"]

    if payload.max_packs > 5:
        raise HTTPException(
            status_code=422,
            detail="max_packs no puede ser mayor a 5",
        )
    if payload.max_packs < 0:
        raise HTTPException(
            status_code=422,
            detail="max_packs debe ser mayor o igual a 0",
        )

    try:
        await plan_limits_repo.db_set_auto_recharge(
            org_id=org_id,
            enabled=payload.enabled,
            max_packs=payload.max_packs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("billing_subscription.set_auto_recharge_error", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al actualizar auto-recharge") from exc

    return {
        "success": True,
        "auto_recharge": {
            "enabled":   payload.enabled,
            "max_packs": payload.max_packs,
        },
    }


@router.post("/buy-pack")
async def buy_pack(
    payload: BuyPackPayload,
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Manually purchase a usage pack (100 conversation credits = $50,000 COP).

    # TODO: Wire Wompi payment before go-live. Currently creates the pack row
    # immediately without charging. PM will integrate Wompi recurring billing
    # once the pricing model is validated with the first paying client.
    # When Wompi is wired: validate the payment token here before inserting
    # the pack row, and record the transaction reference in usage_packs.

    Raises 429 if the org has already reached max_packs_per_month for this period.
    """
    org_id: int = restaurant["id"]

    # Check auto-recharge limit (also applies to manual purchases)
    try:
        # Get the org's max_packs config
        sub = await plan_limits_repo.db_get_org_subscription(org_id)
        max_packs = int(sub.get("auto_recharge_max_packs_per_month") or 5)

        packs_this_period = await plan_limits_repo.db_count_packs_this_period(org_id)
        if packs_this_period >= max_packs:
            raise HTTPException(
                status_code=429,
                detail=f"Límite de {max_packs} packs por período alcanzado. "
                       "Aumentá max_packs en auto-recharge para comprar más.",
            )

        pack_id = await plan_limits_repo.db_create_pack(
            org_id=org_id,
            credits=100,
            amount_paid_cop=50_000,
            fired_automatically=False,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("billing_subscription.buy_pack_error", org_id=org_id)
        raise HTTPException(status_code=500, detail="Error al crear el pack") from exc

    log.info(
        "billing_subscription.pack_purchased",
        org_id=org_id, pack_id=pack_id,
        note=payload.note,
    )
    return {
        "success": True,
        "pack_id": pack_id,
        "credits": 100,
        "amount_paid_cop": 50_000,  # integer COP — no Decimal needed
        "message": (
            "Pack creado. Nota: el cobro por Wompi está pendiente de integración. "
            "Este pack fue acreditado inmediatamente."
        ),
    }


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
