"""Plan limit enforcement guards.

Each `enforce_*` helper raises UsageLimitExceeded when the current usage
(plus any projected delta) would exceed the plan limit. Callers in HTTP
routes catch the exception and translate to HTTP 429; the bot runtime
lets it propagate and replies with a generic technical-error message.

All guards expect:
  - `org_id` int — the canonical tenant key.
  - `restaurant` dict — must include `subscription_plan` and `features`
    (as already returned by db_get_restaurant_by_id / db_get_org_by_id).

Each guard reads usage via subscription_repo, which requires an active
tenant_scope() (or bypass_tenant_scope) — same as every other tenant repo.
"""

import json

from app.services.database import UsageLimitExceeded
from app.repositories import subscription_repo
from app.services.logging import get_logger

log = get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _features_dict(restaurant: dict) -> dict:
    """Normalise restaurant.features into a plain dict.

    asyncpg / our VIEW may return JSONB as either a dict or a JSON string,
    depending on driver and query path. Tolerate both shapes; on parse error
    return {} so the limit lookup falls back to plan defaults.
    """
    feats = (restaurant or {}).get("features") or {}
    if isinstance(feats, str):
        try:
            feats = json.loads(feats)
        except Exception:
            return {}
    return feats if isinstance(feats, dict) else {}


def _plan(restaurant: dict) -> str:
    return (restaurant or {}).get("subscription_plan") or "basic"


# ── Guards ───────────────────────────────────────────────────────────────────


async def enforce_invoice_limit(org_id: int, restaurant: dict) -> None:
    """Raise UsageLimitExceeded if today's invoice count would exceed the plan limit.

    Called BEFORE emitting an invoice. The actual increment happens via
    subscription_repo.db_increment_invoices in the success path.
    """
    feats = _features_dict(restaurant)
    plan = _plan(restaurant)
    limits = subscription_repo._resolve_limits(feats, plan)
    cap = limits["daily_invoices"]
    if cap == -1:  # unlimited
        return
    summary = await subscription_repo.db_get_usage_summary(org_id)
    used = summary["today"]["invoices_used"]
    if used >= cap:
        log.warning(
            "subscription.invoice_limit_hit",
            org_id=org_id, plan=plan, used=used, limit=cap,
        )
        raise UsageLimitExceeded("facturas", used, cap)


async def enforce_token_limit(org_id: int, restaurant: dict, additional: int = 0) -> None:
    """Raise UsageLimitExceeded if (today + additional) tokens would exceed the limit.

    `additional` lets callers pre-charge for an upcoming Claude call. Pass 0
    for a pure post-hoc check. Negative values are ignored (treated as 0).
    """
    feats = _features_dict(restaurant)
    plan = _plan(restaurant)
    limits = subscription_repo._resolve_limits(feats, plan)
    cap = limits["daily_tokens"]
    if cap == -1:
        return
    summary = await subscription_repo.db_get_usage_summary(org_id)
    used = summary["today"]["tokens_used"]
    projected = used + max(0, additional)
    if projected > cap:
        log.warning(
            "subscription.token_limit_hit",
            org_id=org_id, plan=plan, used=used,
            additional=additional, limit=cap,
        )
        raise UsageLimitExceeded("tokens", projected, cap)


async def enforce_order_limit(org_id: int, restaurant: dict) -> None:
    """Raise UsageLimitExceeded if monthly order count >= plan limit.

    Uses >= because the act of creating one more order would push the count
    above the cap. The increment itself happens after a successful commit.
    """
    feats = _features_dict(restaurant)
    plan = _plan(restaurant)
    limits = subscription_repo._resolve_limits(feats, plan)
    cap = limits["monthly_orders"]
    if cap == -1:
        return
    summary = await subscription_repo.db_get_usage_summary(org_id)
    used = summary["month"]["orders_count"]
    if used >= cap:
        log.warning(
            "subscription.order_limit_hit",
            org_id=org_id, plan=plan, used=used, limit=cap,
        )
        raise UsageLimitExceeded("órdenes", used, cap)
