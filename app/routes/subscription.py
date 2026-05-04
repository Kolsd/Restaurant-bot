"""Subscription usage endpoint — current consumption + plan limits.

Exposes a single read endpoint for the admin UI to render usage gauges:

  GET /api/subscription/usage
    → {
        "plan":   "basic",
        "limits": {"daily_tokens": 50000, "daily_invoices": 50, "monthly_orders": 500},
        "usage":  {
          "today": {"tokens_used": 1234, "invoices_used": 3, "orders_count": 12},
          "month": {"orders_count": 87}
        }
      }

Auth: standard restaurant admin token via get_current_restaurant_scoped
(activates tenant_scope for the underlying repo reads).
"""

from fastapi import APIRouter, Depends

from app.routes.deps import get_current_restaurant_scoped
from app.repositories import subscription_repo
from app.services.subscription_guard import _features_dict, _plan
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/subscription", tags=["subscription"])


@router.get("/usage")
async def get_subscription_usage(
    restaurant: dict = Depends(get_current_restaurant_scoped),
):
    """Return the authenticated restaurant's plan, limits, and current usage."""
    feats = _features_dict(restaurant)
    plan = _plan(restaurant)
    limits = subscription_repo._resolve_limits(feats, plan)
    summary = await subscription_repo.db_get_usage_summary(restaurant["id"])
    return {
        "plan":   plan,
        "limits": limits,
        "usage":  summary,
    }
