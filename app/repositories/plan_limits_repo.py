"""app/repositories/plan_limits_repo.py

Repository for plan limits, addon modules, and per-org subscription tracking.

Tables:
  - plan_limits     — global lookup, no RLS (public; bypass for reads)
  - addon_modules   — global lookup, no RLS (public; bypass for reads)
  - usage_packs     — tenant-scoped, RLS org_isolation
  - organizations   — subscription columns (plan_code, active_addons, ...)

Tenant-scoped functions require an active tenant_scope(org_id) at the call
site; global lookups use bypass_tenant_scope("global_lookup_plans").

Money: prices are INTEGER (COP zero-decimal) in the DB — no Decimal needed for
display. Audio usage is NUMERIC(10,2) → Decimal in Python, float at JSON boundary.

Status thresholds for db_check_caps:
  ok      — used < 50% of cap
  warn50  — 50% ≤ used < 80%
  warn80  — 80% ≤ used < 90%
  warn90  — 90% ≤ used < 100%
  exceeded — used ≥ cap
  comp    — comp_until is set and in the future (all dimensions return 'comp')
"""
from __future__ import annotations

from datetime import datetime, timezone, date
from decimal import Decimal
from typing import Any

from app.services.logging import get_logger
from app.services.money import to_decimal, quantize_money
from app.services.tenant_context import bypass_tenant_scope, bypass_tenant_scope_if_unset
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _row_to_dict(row) -> dict:
    """Convert asyncpg Record to a plain serialisable dict."""
    if row is None:
        return {}
    d: dict[str, Any] = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, date):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(quantize_money(v))  # JSON boundary
        else:
            d[k] = v
    return d


def _cap_status(used: int | Decimal, cap: int) -> str:
    """Return threshold status string for a single usage dimension."""
    if cap <= 0:
        # Treat 0-cap as unlimited (should not occur with valid plans)
        return "ok"
    used_num = int(used) if isinstance(used, int) else float(used)
    pct = used_num / cap
    if pct >= 1.0:
        return "exceeded"
    if pct >= 0.90:
        return "warn90"
    if pct >= 0.80:
        return "warn80"
    if pct >= 0.50:
        return "warn50"
    return "ok"


# ── Global lookup functions ───────────────────────────────────────────────────


async def db_get_plan(plan_code: str) -> dict | None:
    """Fetch a single plan by code. Global — no tenant required.

    Uses bypass_tenant_scope_if_unset("global_lookup_plans") — the SOFT variant.
    This function is called both standalone (no scope active) AND from within
    tenant-scoped call paths (db_set_plan, db_request_downgrade are both
    "Requires active tenant_scope(org_id)" and both call db_get_plan to validate
    the target plan exists). A strict bypass_tenant_scope() would raise
    TenantContextConflict whenever a tenant is already pinned — plan_limits has
    no RLS policy at all, so there is nothing to bypass in that case; the soft
    variant simply no-ops and lets the existing scope's connection read the
    (unfiltered) global table.
    """
    with bypass_tenant_scope_if_unset("global_lookup_plans"):
        async with tenant_connection() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM plan_limits WHERE plan_code = $1",
                plan_code,
            )
    return _row_to_dict(row) if row else None


async def db_list_plans() -> list[dict]:
    """List all plans ordered by sort_order. Global — no tenant required.

    Uses bypass_tenant_scope_if_unset("global_lookup_plans") — see db_get_plan
    docstring. Called both from unauthenticated routes (no scope) and from
    scoped routes such as GET /api/billing/plan-options.
    """
    with bypass_tenant_scope_if_unset("global_lookup_plans"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM plan_limits ORDER BY sort_order ASC, plan_code ASC"
            )
    return [_row_to_dict(r) for r in rows]


async def db_list_addons() -> list[dict]:
    """List all addon modules ordered by sort_order. Global — no tenant required.

    Uses bypass_tenant_scope_if_unset("global_lookup_plans") — see db_get_plan
    docstring.
    """
    with bypass_tenant_scope_if_unset("global_lookup_plans"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                "SELECT * FROM addon_modules ORDER BY sort_order ASC, module_code ASC"
            )
    return [_row_to_dict(r) for r in rows]


# ── Tenant-scoped subscription functions ─────────────────────────────────────


async def db_get_org_subscription(org_id: int) -> dict:
    """Return current plan + addons + auto-recharge config + period usage for an org.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        org_row = await conn.fetchrow(
            """
            SELECT o.plan_code, o.active_addons, o.auto_recharge_enabled,
                   o.auto_recharge_max_packs_per_month,
                   o.current_period_start, o.current_period_convs_used,
                   o.current_period_audio_min_used, o.comp_until,
                   o.annual_billing,
                   pl.display_name AS plan_display_name,
                   pl.monthly_price_cop, pl.conv_cap, pl.audio_min_cap,
                   pl.storage_mb_cap, pl.locations_included, pl.staff_cap,
                   pl.sku_cap, pl.marketing_msg_cap
            FROM organizations o
            LEFT JOIN plan_limits pl ON pl.plan_code = o.plan_code
            WHERE o.id = $1
            """,
            org_id,
        )
    if org_row is None:
        return {}
    return _row_to_dict(org_row)


async def db_increment_conv_usage(org_id: int, count: int = 1) -> int:
    """Atomically increment conv usage for the current period.

    Returns the new current_period_convs_used value.
    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        new_val = await conn.fetchval(
            """
            UPDATE organizations
               SET current_period_convs_used = current_period_convs_used + $2
             WHERE id = $1
            RETURNING current_period_convs_used
            """,
            org_id, count,
        )
    result = int(new_val or 0)
    log.debug("plan_limits.conv_incremented", org_id=org_id, count=count, new_total=result)
    return result


async def db_check_caps(org_id: int) -> dict:
    """Return per-dimension cap status for an org.

    Dimensions: conv, audio.
    Pack credits are included in conv available calculation (FIFO, non-expired).

    Status values per dimension:
      ok, warn50, warn80, warn90, exceeded, comp

    If comp_until > NOW(), all dimensions return status='comp'.

    Return shape:
      {
        "comp_active": bool,
        "conv": {"used": int, "cap": int, "pack_credits": int, "available": int, "pct": float, "status": str},
        "audio": {"used": float, "cap": int, "pct": float, "status": str},
      }

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        org_row = await conn.fetchrow(
            """
            SELECT o.current_period_convs_used, o.current_period_audio_min_used,
                   o.comp_until,
                   pl.conv_cap, pl.audio_min_cap
            FROM organizations o
            LEFT JOIN plan_limits pl ON pl.plan_code = o.plan_code
            WHERE o.id = $1
            """,
            org_id,
        )
        if org_row is None:
            return {}

        # Pack credits: sum remaining, non-expired
        pack_credits_val = await conn.fetchval(
            """
            SELECT COALESCE(SUM(credits_remaining), 0)
            FROM usage_packs
            WHERE org_id = $1
              AND credits_remaining > 0
              AND expires_at > NOW()
            """,
            org_id,
        )

    pack_credits = int(pack_credits_val or 0)

    comp_until = org_row["comp_until"]
    comp_active = (
        comp_until is not None
        and comp_until > datetime.now(tz=timezone.utc)
    )

    conv_used = int(org_row["current_period_convs_used"] or 0)
    conv_cap = int(org_row["conv_cap"] or 0)
    conv_available = max(0, conv_cap - conv_used) + pack_credits
    conv_pct = (conv_used / conv_cap) if conv_cap > 0 else 0.0

    audio_used_decimal = to_decimal(org_row["current_period_audio_min_used"])
    audio_cap = int(org_row["audio_min_cap"] or 0)
    audio_pct = (float(audio_used_decimal) / audio_cap) if audio_cap > 0 else 0.0

    if comp_active:
        conv_status = "comp"
        audio_status = "comp"
    else:
        conv_status = _cap_status(conv_used, conv_cap)
        audio_status = _cap_status(audio_used_decimal, audio_cap)

    return {
        "comp_active": comp_active,
        "conv": {
            "used": conv_used,
            "cap": conv_cap,
            "pack_credits": pack_credits,
            "available": conv_available,
            "pct": round(conv_pct, 4),
            "status": conv_status,
        },
        "audio": {
            "used": float(quantize_money(audio_used_decimal)),  # JSON boundary
            "cap": audio_cap,
            "pct": round(audio_pct, 4),
            "status": audio_status,
        },
    }


async def db_reset_period(org_id: int) -> None:
    """Reset current period usage counters and advance current_period_start to today.

    Called from the billing scheduler when a new billing period begins.
    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE organizations
               SET current_period_start = CURRENT_DATE,
                   current_period_convs_used = 0,
                   current_period_audio_min_used = 0
             WHERE id = $1
            """,
            org_id,
        )
    log.info("plan_limits.period_reset", org_id=org_id)


async def db_set_plan(
    org_id: int,
    plan_code: str,
    active_addons: list[str] | None = None,
) -> None:
    """Change the plan (and optionally the active addons) for an org.

    Raises ValueError if plan_code is not found in plan_limits.
    # Requires active tenant_scope(org_id).
    """
    # Validate plan exists (use bypass since plan_limits is a global table)
    plan = await db_get_plan(plan_code)
    if plan is None:
        raise ValueError(f"Unknown plan_code: {plan_code!r}")

    async with tenant_connection() as conn:
        if active_addons is not None:
            await conn.execute(
                """
                UPDATE organizations
                   SET plan_code = $2, active_addons = $3
                 WHERE id = $1
                """,
                org_id, plan_code, active_addons,
            )
        else:
            await conn.execute(
                "UPDATE organizations SET plan_code = $2 WHERE id = $1",
                org_id, plan_code,
            )
    log.info(
        "plan_limits.plan_changed",
        org_id=org_id, plan_code=plan_code,
        addons=active_addons,
    )


async def db_set_comp_until(
    org_id: int,
    comp_until: datetime | None,
) -> None:
    """Set or clear the comp_until timestamp for an org (friends & family).

    Pass None to clear the comp override.
    # Requires active tenant_scope(org_id) or bypass for internal admin routes.
    """
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE organizations SET comp_until = $2 WHERE id = $1",
            org_id, comp_until,
        )
    log.info("plan_limits.comp_until_set", org_id=org_id, comp_until=str(comp_until))


# ── Pending plan downgrade ────────────────────────────────────────────────────

# Plan sort order for downgrade validation (lower index = smaller plan).
_PLAN_ORDER = ["pulso", "restaurante", "pro", "cadena"]


def _plan_rank(plan_code: str) -> int:
    """Return numeric rank of a plan (lower = smaller). Unknown plans get rank 999."""
    try:
        return _PLAN_ORDER.index(plan_code.lower())
    except ValueError:
        return 999


async def db_request_downgrade(
    org_id: int,
    new_plan_code: str,
    kept_location_id: int,
) -> dict:
    """Schedule a plan downgrade for an org, effective in 7 days.

    Validates:
    - new_plan_code must exist in plan_limits.
    - new_plan_code must be SMALLER than the current plan (downgrades only).
    - kept_location_id must belong to this org.

    Sets the three pending_* columns and returns the updated state.
    # Requires active tenant_scope(org_id).
    """
    from datetime import timedelta  # noqa: PLC0415

    # Validate new plan exists (global table — bypass)
    plan = await db_get_plan(new_plan_code)
    if plan is None:
        raise ValueError(f"Unknown plan_code: {new_plan_code!r}")

    async with tenant_connection() as conn:
        # Read current plan
        current_row = await conn.fetchrow(
            "SELECT plan_code FROM organizations WHERE id = $1",
            org_id,
        )
        if current_row is None:
            raise ValueError(f"org_id {org_id} not found")
        current_plan = current_row["plan_code"] or "pulso"

        if _plan_rank(new_plan_code) >= _plan_rank(current_plan):
            raise ValueError(
                f"Downgrade only: {new_plan_code!r} is not smaller than current plan {current_plan!r}. "
                "Use a different endpoint to upgrade."
            )

        # Verify kept_location_id belongs to this org
        loc_row = await conn.fetchrow(
            "SELECT id FROM locations WHERE id = $1 AND org_id = $2",
            kept_location_id, org_id,
        )
        if loc_row is None:
            raise ValueError(
                f"kept_location_id {kept_location_id} does not belong to org {org_id}"
            )

        effective_at = datetime.now(tz=timezone.utc) + timedelta(days=7)
        row = await conn.fetchrow(
            """
            UPDATE organizations
               SET pending_plan_code         = $2,
                   pending_plan_effective_at = $3,
                   pending_kept_location_id  = $4
             WHERE id = $1
            RETURNING plan_code, pending_plan_code,
                      pending_plan_effective_at, pending_kept_location_id
            """,
            org_id, new_plan_code, effective_at, kept_location_id,
        )

    result = _row_to_dict(row) if row else {}
    log.info(
        "plan_limits.downgrade_requested",
        org_id=org_id,
        new_plan=new_plan_code,
        effective_at=effective_at.isoformat(),
        kept_location_id=kept_location_id,
    )
    return result


async def db_cancel_downgrade(org_id: int) -> bool:
    """Clear any pending downgrade for an org.

    Returns True if a pending downgrade existed (and was cleared), False
    if there was nothing to cancel.
    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE organizations
               SET pending_plan_code         = NULL,
                   pending_plan_effective_at = NULL,
                   pending_kept_location_id  = NULL
             WHERE id = $1
               AND pending_plan_code IS NOT NULL
            RETURNING id
            """,
            org_id,
        )
    existed = row is not None
    if existed:
        log.info("plan_limits.downgrade_cancelled", org_id=org_id)
    return existed


async def db_get_pending_downgrade(org_id: int) -> dict | None:
    """Return the pending downgrade state for an org, or None if none.

    # Requires active tenant_scope(org_id).
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT plan_code, pending_plan_code,
                   pending_plan_effective_at, pending_kept_location_id
            FROM organizations
            WHERE id = $1
              AND pending_plan_code IS NOT NULL
            """,
            org_id,
        )
    if row is None:
        return None
    return _row_to_dict(row)


async def db_apply_due_downgrades() -> list[dict]:
    """Apply all pending downgrades whose effective_at has passed.

    For each qualifying org:
    - Sets plan_code = pending_plan_code
    - Clears the three pending_* fields

    Cross-tenant — MUST be called under bypass_tenant_scope("scheduler_apply_downgrades").

    Returns a list of dicts describing each org that was processed.
    """
    with bypass_tenant_scope("scheduler_apply_downgrades"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                UPDATE organizations
                   SET plan_code                 = pending_plan_code,
                       pending_plan_code         = NULL,
                       pending_plan_effective_at = NULL,
                       pending_kept_location_id  = NULL
                 WHERE pending_plan_code IS NOT NULL
                   AND pending_plan_effective_at <= NOW()
                RETURNING id, plan_code, pending_kept_location_id
                """,
            )
    results = [_row_to_dict(r) for r in rows]
    if results:
        log.info(
            "plan_limits.downgrades_applied",
            count=len(results),
            org_ids=[r.get("id") for r in results],
        )
    return results
