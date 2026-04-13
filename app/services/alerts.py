"""
Operational alert service.

Checks health metrics periodically and dispatches alerts via
configurable channels (log + optional webhook).

Called by the scheduler every tick (~60s).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx

from app.services.logging import get_logger

log = get_logger(__name__)

# Alert cooldowns — don't spam the same alert within this window
_COOLDOWN_SECONDS = 300  # 5 minutes

# In-memory tracking of last alert time per alert_key
_last_alert: dict[str, float] = {}

# Thresholds
_P95_LATENCY_THRESHOLD_MS = 500.0
_QUEUE_DEPTH_THRESHOLD = 50
_ERROR_RATE_THRESHOLD = 0.10  # 10%


async def check_alerts() -> None:
    """Run all health checks and fire alerts if thresholds breached.

    Called by the scheduler every tick (~60s).
    Errors are caught internally — this must never crash the scheduler.
    """
    try:
        await _check_dead_letters()
    except Exception:
        log.exception("alerts.check_dead_letters_failed")

    try:
        await _check_pool_exhaustion()
    except Exception:
        log.exception("alerts.check_pool_exhaustion_failed")

    try:
        await _check_inbox_latency()
    except Exception:
        log.exception("alerts.check_inbox_latency_failed")

    try:
        await _check_queue_depth()
    except Exception:
        log.exception("alerts.check_queue_depth_failed")

    try:
        await _check_error_rate()
    except Exception:
        log.exception("alerts.check_error_rate_failed")


# ── Individual checks ─────────────────────────────────────────────────────────

async def _check_dead_letters() -> None:
    from app.services.database import get_pool  # late import — avoids circular

    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM webhook_inbox WHERE last_error LIKE 'DEAD_LETTER:%'"
        )
    count = count or 0
    if count > 0:
        await _fire_alert(
            key="dead_letters",
            severity="HIGH",
            title="Dead Letters Detected",
            detail=f"{count} message(s) permanently failed in webhook_inbox.",
        )


async def _check_pool_exhaustion() -> None:
    from app.services.database import get_pool  # late import

    pool = await get_pool()
    free = pool.get_idle_size()
    if free == 0:
        total = pool.get_size()
        await _fire_alert(
            key="db_pool_exhausted",
            severity="CRITICAL",
            title="DB Pool Exhausted",
            detail=f"0 free connections out of {total}.",
        )


async def _check_inbox_latency() -> None:
    from app.services.inbox_worker import get_metrics  # late import

    metrics = get_metrics()
    p95_ms = metrics.get("inbox_latency_p95_ms", 0.0)
    if p95_ms > _P95_LATENCY_THRESHOLD_MS:
        await _fire_alert(
            key="high_inbox_latency",
            severity="MEDIUM",
            title="High Inbox Latency",
            detail=f"p95 latency is {p95_ms:.1f}ms (threshold: {_P95_LATENCY_THRESHOLD_MS}ms).",
        )


async def _check_queue_depth() -> None:
    from app.services.database import get_pool  # late import

    pool = await get_pool()
    async with pool.acquire() as conn:
        depth = await conn.fetchval(
            "SELECT COUNT(*) FROM webhook_inbox WHERE processed_at IS NULL"
        )
    depth = depth or 0
    if depth > _QUEUE_DEPTH_THRESHOLD:
        await _fire_alert(
            key="inbox_queue_backup",
            severity="HIGH",
            title="Inbox Queue Backup",
            detail=f"Queue depth is {depth} (threshold: {_QUEUE_DEPTH_THRESHOLD}).",
        )


async def _check_error_rate() -> None:
    from app.services.inbox_worker import get_metrics  # late import

    metrics = get_metrics()
    processed = metrics.get("inbox_processed_total", 0)
    errors = metrics.get("inbox_errors_total", 0)
    total = processed + errors
    if total == 0:
        return  # No traffic — nothing to check
    rate = errors / total
    if rate > _ERROR_RATE_THRESHOLD:
        pct = rate * 100
        await _fire_alert(
            key="worker_error_spike",
            severity="HIGH",
            title="Worker Error Rate Spike",
            detail=f"Error rate is {pct:.1f}% ({errors}/{total}) — threshold: {_ERROR_RATE_THRESHOLD * 100:.0f}%.",
        )


# ── Alert dispatcher ──────────────────────────────────────────────────────────

async def _fire_alert(key: str, severity: str, title: str, detail: str) -> None:
    """Dispatch an alert if not in cooldown.

    1. Checks cooldown using time.monotonic().
    2. Logs via structlog at WARNING level.
    3. If ALERT_WEBHOOK_URL env var is set, POSTs to it (fire-and-forget).
    """
    now = time.monotonic()
    last = _last_alert.get(key, 0.0)
    if now - last < _COOLDOWN_SECONDS:
        # Still in cooldown — suppress
        return

    _last_alert[key] = now

    log.warning(
        "alert.fired",
        key=key,
        severity=severity,
        title=title,
        detail=detail,
    )

    webhook_url = os.getenv("ALERT_WEBHOOK_URL", "")
    if webhook_url:
        await _post_webhook(webhook_url, key=key, severity=severity, title=title, detail=detail)


async def _post_webhook(
    url: str, *, key: str, severity: str, title: str, detail: str
) -> None:
    """POST alert payload to the configured webhook (Slack / Discord / custom).

    Fire-and-forget — errors are logged but never re-raised.
    """
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "text": f"[{severity}] {title} — {detail}",
        "severity": severity,
        "key": key,
        "timestamp": timestamp,
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                log.warning(
                    "alert.webhook_bad_response",
                    key=key,
                    status_code=resp.status_code,
                )
    except Exception:
        log.exception("alert.webhook_post_failed", key=key, url=url)
