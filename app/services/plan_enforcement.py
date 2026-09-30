"""
app/services/plan_enforcement.py

Subscription cap enforcement for the bot pipeline.

One conversation slot = one inbound customer message (not one LLM call, not one session).
Tool-use chains within a single inbound message consume exactly 1 slot.

Decision flow per message:
  1. comp tenant          → PROCEED unconditionally (friends & family)
  2. under cap            → PROCEED, increment usage, fire threshold warn if crossing 50/80/90%
  3. exceeded + pack credits → consume 1 credit, PROCEED
  4. exceeded + auto-recharge eligible → create new pack, consume 1 credit, PROCEED
  5. all options exhausted → REDIRECT_TO_HUMAN

Errors in cap check NEVER silence the bot — fail-open: log, return PROCEED.
Errors in auto-recharge db_create_pack → REDIRECT (no proceed without cap room).

Threshold notifications (50/80/90% crossed) are best-effort: failures are logged
but never propagated. Dedup via Redis key per (org_id, period_start, threshold pct).

Multi-worker safe: all state is in Postgres (atomic UPDATE RETURNING) + Redis for dedup.
"""
from __future__ import annotations

import enum
from datetime import timezone

from app.services.logging import get_logger
from app.repositories.plan_limits_repo import (
    db_check_caps,
    db_increment_conv_usage,
    db_create_pack,
    db_count_packs_this_period,
    db_consume_pack_credit,
    db_get_org_subscription,
)

log = get_logger(__name__)

# ── Public API ────────────────────────────────────────────────────────────────


class CapDecision(enum.Enum):
    PROCEED = "proceed"
    REDIRECT_TO_HUMAN = "redirect_to_human"


# Customer-facing message when cap is exhausted
REDIRECT_MESSAGE = (
    "Hola 👋 En este momento te atiende un asesor humano. "
    "Espera un momentico por favor, ya te respondemos."
)

# Usage percentages logged once per period. The owner used to get these by
# WhatsApp (retired 2026-09-25); Mesio sees caps in /internal notifications.
_WARN_THRESHOLDS = (50, 80, 90)


async def check_and_consume_conv_slot(
    org_id: int,
) -> CapDecision:
    """Check cap and consume one conversation slot.

    Must be called INSIDE tenant_scope(org_id) — agent.chat() runs inside the
    scope its caller (the diner chat route) set.

    Bot-rule constraint: errors in cap infrastructure NEVER silence the bot.
    An exception from db_check_caps or db_increment_conv_usage causes a PROCEED
    (fail-open) with a log.exception. The customer must never see a cap-check error.

    Args:
        org_id:       Organization ID (tenant key, equals restaurant_obj["id"] post-Wave-2).

    Returns:
        CapDecision.PROCEED           — allow message to continue to LLM
        CapDecision.REDIRECT_TO_HUMAN — reply with redirect message, skip LLM
    """
    try:
        caps = await db_check_caps(org_id)
    except Exception:
        log.exception(
            "plan_enforcement.caps_check_failed_fail_open",
            org_id=org_id,
            note="proceeding to avoid silencing the bot",
        )
        return CapDecision.PROCEED

    if not caps:
        # No plan configured → treat as unlimited (pre-launch / setup phase)
        log.debug("plan_enforcement.no_caps_data_proceed", org_id=org_id)
        return CapDecision.PROCEED

    conv = caps.get("conv", {})
    conv_status = conv.get("status", "ok")

    # ── Comp tenant: bypass all cap math ──────────────────────────────────────
    if conv_status == "comp":
        try:
            new_used = await db_increment_conv_usage(org_id)
        except Exception:
            log.exception("plan_enforcement.increment_failed_comp", org_id=org_id)
        log.debug("plan_enforcement.comp_proceed", org_id=org_id)
        return CapDecision.PROCEED

    # ── Under cap: normal path ────────────────────────────────────────────────
    if conv_status != "exceeded":
        try:
            new_used = await db_increment_conv_usage(org_id)
        except Exception:
            log.exception("plan_enforcement.increment_failed_undercap", org_id=org_id)
            return CapDecision.PROCEED

        # Fire threshold warnings (best-effort side-effect)
        _fire_threshold_warn_nowait(org_id, new_used, caps)

        return CapDecision.PROCEED

    # ── Cap exceeded: try pack credits first ──────────────────────────────────
    pack_credits_available = conv.get("pack_credits", 0)
    if pack_credits_available > 0:
        try:
            consumed = await db_consume_pack_credit(org_id, count=1)
            if consumed > 0:
                try:
                    await db_increment_conv_usage(org_id)
                except Exception:
                    log.exception("plan_enforcement.increment_failed_pack_credit", org_id=org_id)
                log.info(
                    "plan_enforcement.pack_credit_consumed",
                    org_id=org_id,
                    credits_consumed=consumed,
                )
                return CapDecision.PROCEED
        except Exception:
            log.exception("plan_enforcement.consume_pack_failed", org_id=org_id)
            # Fall through to auto-recharge check

    # ── Try auto-recharge ─────────────────────────────────────────────────────
    try:
        sub = await db_get_org_subscription(org_id)
    except Exception:
        log.exception("plan_enforcement.get_sub_failed", org_id=org_id)
        sub = {}

    auto_recharge_enabled = sub.get("auto_recharge_enabled", False)
    max_packs = sub.get("auto_recharge_max_packs_per_month", 0)

    if auto_recharge_enabled and max_packs > 0:
        try:
            packs_used = await db_count_packs_this_period(org_id)
        except Exception:
            log.exception("plan_enforcement.count_packs_failed", org_id=org_id)
            packs_used = max_packs  # assume maxed out on error → don't recharge

        if packs_used < max_packs:
            # Auto-recharge: create pack + consume 1 credit
            try:
                await db_create_pack(
                    org_id,
                    credits=100,
                    amount_paid_cop=50_000,
                    fired_automatically=True,
                )
            except Exception:
                log.exception("plan_enforcement.create_pack_failed", org_id=org_id)
                # If pack creation fails, we cannot safely proceed (no cap room)
                return CapDecision.REDIRECT_TO_HUMAN

            try:
                await db_consume_pack_credit(org_id, count=1)
            except Exception:
                log.exception("plan_enforcement.consume_after_create_failed", org_id=org_id)

            try:
                await db_increment_conv_usage(org_id)
            except Exception:
                log.exception("plan_enforcement.increment_after_recharge_failed", org_id=org_id)

            log.info(
                "plan_enforcement.auto_recharged",
                org_id=org_id,
                packs_used=packs_used + 1,
                max_packs=max_packs,
            )

            return CapDecision.PROCEED

    # ── All options exhausted ─────────────────────────────────────────────────
    log.warning(
        "plan_enforcement.cap_exhausted_redirect",
        org_id=org_id,
        conv_used=conv.get("used"),
        conv_cap=conv.get("cap"),
        pack_credits=conv.get("pack_credits"),
        auto_recharge_enabled=auto_recharge_enabled,
    )
    return CapDecision.REDIRECT_TO_HUMAN


# ── Threshold notification helpers ────────────────────────────────────────────

def _fire_threshold_warn_nowait(org_id: int, new_used: int, caps: dict) -> None:
    """Schedule threshold warning as a fire-and-forget asyncio task (best-effort)."""
    import asyncio
    try:
        asyncio.ensure_future(
            _fire_threshold_warn(org_id, new_used, caps)
        )
    except Exception:
        pass  # best-effort; never block the bot


async def _fire_threshold_warn(org_id: int, new_used: int, caps: dict) -> None:
    """Log once per period when usage crosses 50/80/90%."""
    try:
        conv = caps.get("conv", {})
        cap = conv.get("cap", 0)
        if cap <= 0:
            return

        # Determine which threshold was just crossed
        crossed_pct: int | None = None
        prev_used = new_used - 1  # the value before this increment

        for pct in _WARN_THRESHOLDS:
            threshold_count = int(cap * pct / 100)
            if prev_used < threshold_count <= new_used:
                crossed_pct = pct
                break  # only the lowest newly-crossed threshold

        if crossed_pct is None:
            return

        # Dedup via Redis: only the first time per (org, period, threshold)
        dedup_acquired = await _acquire_threshold_warn_slot(org_id, crossed_pct)
        if not dedup_acquired:
            log.debug(
                "plan_enforcement.threshold_warn_dedup_skip",
                org_id=org_id, pct=crossed_pct,
            )
            return

        log.warning(
            "plan_enforcement.usage_threshold_crossed",
            org_id=org_id, pct=crossed_pct, used=new_used, cap=cap,
        )

    except Exception:
        log.warning("plan_enforcement.threshold_warn_failed", org_id=org_id, exc_info=True)


async def _acquire_threshold_warn_slot(org_id: int, pct: int) -> bool:
    """Return True if this is the first notification for (org, pct) in the current period.

    Uses Redis SET NX (no expiry — key survives until usage resets at period start,
    when org_id's conv counter resets). Fallback: always True (fire every time, acceptable).
    """
    from app.services import redis_client as _rc  # noqa: PLC0415

    key = f"mesio:plan_warn:{org_id}:{pct}"
    try:
        r = await _rc.get_redis()
        if r is not None:
            # SET NX without expiry — period reset (db_reset_period) should del these keys.
            # TTL of 35 days covers the longest possible billing month.
            result = await r.set(key, "1", nx=True, ex=35 * 86400)
            return result is not None
    except Exception:
        log.warning("plan_enforcement.threshold_dedup_redis_failed", org_id=org_id, pct=pct)
    # Fallback: in-process dict
    return _fb_acquire_threshold_warn(org_id, pct)


# In-process fallback for threshold dedup (single-worker only)
_fb_threshold_warns: dict[str, bool] = {}


def _fb_acquire_threshold_warn(org_id: int, pct: int) -> bool:
    key = f"{org_id}:{pct}"
    if key in _fb_threshold_warns:
        return False
    _fb_threshold_warns[key] = True
    return True
