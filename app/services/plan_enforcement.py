"""
app/services/plan_enforcement.py

Conversation metering for the bot pipeline.

One conversation = one inbound customer message (not one LLM call, not one
session). Tool-use chains within a single inbound message count once.

PM decision 2026-09-23 (docs/claude/status.md #14d): Mesio sells a flat
price per sede. The plan's conversation allowance is an INTERNAL soft
ceiling: crossing it alerts Mesio (/internal notifications, plan_cap and
cost_runaway), and it never cuts the bot off mid-service. Until 2026-09-29
an exhausted cap made the bot answer "te atiende un asesor humano" and
auto-charged 50.000 COP packs of 100 conversations; both are gone.

Errors here never touch the bot: log and move on.
"""
from __future__ import annotations

from app.services.logging import get_logger
from app.repositories.plan_limits_repo import (
    db_check_caps,
    db_increment_conv_usage,
)

log = get_logger(__name__)

# Usage percentages logged once per period.
_WARN_THRESHOLDS = (50, 80, 90, 100)


async def record_conversation(org_id: int) -> None:
    """Count one conversation for this org and log the thresholds it crosses.

    Must be called INSIDE tenant_scope(org_id) — agent.chat() runs inside the
    scope its caller (the diner chat route) set. Never raises.
    """
    try:
        caps = await db_check_caps(org_id)
        new_used = await db_increment_conv_usage(org_id)
    except Exception:
        log.exception("plan_enforcement.record_failed", org_id=org_id)
        return
    if caps and caps.get("conv", {}).get("status") != "comp":
        _fire_threshold_warn_nowait(org_id, new_used, caps)


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
