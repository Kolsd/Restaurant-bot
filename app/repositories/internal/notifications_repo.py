"""
app/repositories/internal/notifications_repo.py

Aggregates operational notifications for the Mesio HQ Action Queue.
Cross-tenant — callers must wrap in bypass_tenant_scope.

Each source function is best-effort: exceptions are logged and return [].
The aggregator collects results from all sources and sorts by priority.
"""
from __future__ import annotations

from datetime import datetime, date, timezone
from typing import Optional

from app.services.logging import get_logger

log = get_logger(__name__)

# Severity ordering for sort
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Cost runaway threshold — same as alerts.py
_COST_RUNAWAY_MULTIPLIER = 2.0
_CHURN_RATIO_THRESHOLD = 0.5
_CHURN_BASELINE_MIN = 3.0


# ── Shared pool accessor ──────────────────────────────────────────────────────

async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


# ── Source functions ──────────────────────────────────────────────────────────

async def _fetch_cost_runaway() -> list[dict]:
    """Tenants who exceeded 2x their plan daily token budget today."""
    try:
        from app.repositories.cost_metrics_repo import _PLAN_DAILY_TOKEN_LIMITS  # noqa: PLC0415

        from app.services.tenant_db import tenant_connection  # noqa: PLC0415

        today = date.today()
        # subscription_usage has RLS: a bare pool connection (mesio_app)
        # read no rows, so this alert could never fire.
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    su.org_id,
                    o.name                                AS org_name,
                    COALESCE(o.plan_code, 'esencial') AS plan_code,
                    COALESCE(SUM(su.total_tokens), 0)::BIGINT AS tokens_today
                FROM subscription_usage su
                LEFT JOIN organizations o ON o.id = su.org_id
                WHERE su.usage_date = $1
                GROUP BY su.org_id, o.name, o.plan_code
                HAVING COALESCE(SUM(su.total_tokens), 0) > 0
                """,
                today,
            )

        results = []
        for row in rows:
            plan = (row["plan_code"] or "free").lower()
            daily_limit = _PLAN_DAILY_TOKEN_LIMITS.get(plan, _PLAN_DAILY_TOKEN_LIMITS.get("esencial", 50_000))
            if daily_limit <= 0:
                continue  # unlimited plan
            tokens_today = int(row["tokens_today"] or 0)
            if tokens_today <= _COST_RUNAWAY_MULTIPLIER * daily_limit:
                continue
            org_id = row["org_id"]
            org_name = row["org_name"] or f"Org #{org_id}"
            pct = int(tokens_today / daily_limit * 100)
            results.append(
                {
                    "id": f"cost_runaway:{org_id}",
                    "type": "cost",
                    "severity": "high",
                    "title": f"{org_name} superó 2x su presupuesto diario de tokens",
                    "detail": (
                        f"Org #{org_id} ({plan}): {tokens_today:,} tokens hoy "
                        f"({pct}% del límite diario de {daily_limit:,})."
                    ),
                    "url": f"/internal/org/{org_id}",
                    "created_at": _now_iso(),
                    "count": 1,
                    "tenant_id": org_id,
                }
            )
        return results
    except Exception:
        log.exception("notifications_repo.cost_runaway_error")
        return []


async def _fetch_churn_risk() -> list[dict]:
    """Restaurants whose last 7 days of orders (table rounds + web orders)
    fell under half of their daily average of the 14 days before.

    It read `conversations` — WhatsApp-era, and empty for Esencial, which
    has no AI chat — over a bare mesio_app connection where RLS hides every
    row: it never fired. Sales are what a restaurant losing interest stops
    producing, whatever its plan.
    """
    try:
        from app.services.live_demo import DEMO_SLUG  # noqa: PLC0415
        from app.services.tenant_db import tenant_connection  # noqa: PLC0415

        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """
                WITH sales AS (
                    SELECT org_id, created_at FROM table_orders
                     WHERE created_at >= NOW() - INTERVAL '21 days'
                       AND status NOT IN ('cancelled', 'cancelado')
                    UNION ALL
                    SELECT org_id, created_at FROM orders
                     WHERE created_at >= NOW() - INTERVAL '21 days'
                       AND status NOT IN ('cancelado', 'rechazado', 'cancelled')
                ),
                per_org AS (
                    SELECT org_id,
                           COUNT(*) FILTER (WHERE created_at <  NOW() - INTERVAL '7 days') / 14.0 AS baseline_avg,
                           COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') / 7.0  AS recent_avg
                      FROM sales GROUP BY org_id
                )
                SELECT p.org_id, o.name AS org_name, p.baseline_avg::float, p.recent_avg::float
                  FROM per_org p JOIN organizations o ON o.id = p.org_id
                 WHERE p.baseline_avg >= $1
                   AND p.recent_avg < $2 * p.baseline_avg
                   AND COALESCE(o.slug, '') <> $3
                 ORDER BY p.recent_avg / NULLIF(p.baseline_avg, 0) ASC
                """,
                _CHURN_BASELINE_MIN,
                _CHURN_RATIO_THRESHOLD,
                DEMO_SLUG,
            )

        results = []
        for row in rows:
            org_id = row["org_id"]
            org_name = row["org_name"] or f"Org #{org_id}"
            baseline = float(row["baseline_avg"])
            recent = float(row["recent_avg"])
            drop_pct = round((1.0 - recent / baseline) * 100) if baseline > 0 else 100
            results.append(
                {
                    "id": f"churn:{org_id}",
                    "type": "churn",
                    "severity": "medium",
                    "title": f"{org_name}: los pedidos cayeron {drop_pct}%",
                    "detail": (
                        f"{recent:.1f} pedidos/día los últimos 7 días contra "
                        f"{baseline:.1f} las dos semanas anteriores. Llamar al dueño."
                    ),
                    "url": f"/internal/org/{org_id}",
                    "created_at": _now_iso(),
                    "count": 1,
                    "tenant_id": org_id,
                }
            )
        return results
    except Exception:
        log.exception("notifications_repo.churn_risk_error")
        return []


async def _fetch_new_prospects() -> list[dict]:
    """New prospects created in the last 24 hours at stage 'prospecto'."""
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM prospects
                WHERE created_at > NOW() - INTERVAL '24 hours'
                  AND stage = 'prospecto'
                """
            )
        count = int(count or 0)
        if count == 0:
            return []
        return [
            {
                "id": f"new_prospect:{count}",
                "type": "prospect",
                "severity": "low",
                "title": f"{count} prospect{'s' if count != 1 else ''} nuevo{'s' if count != 1 else ''} en las últimas 24h",
                "detail": "Nuevos prospectos en etapa inicial. Revisa el CRM.",
                "url": "/internal/crm",
                "created_at": _now_iso(),
                "count": count,
                "tenant_id": None,
            }
        ]
    except Exception:
        log.exception("notifications_repo.new_prospects_error")
        return []


# _fetch_suspended_tenants and _fetch_billing_attention were removed
# 2026-10-02: the HQ alerts (hq_snapshot flags suspended / overdue /
# trial_ending, kept open by the scheduler) say the same thing with where to
# look and how to fix, and both sources showed up twice in the action queue.
# The first one also read subscription_status alone, not the billing state.


async def _fetch_plan_cap_warnings() -> list[dict]:
    """Tenants at >= 90% of their plan's conversation soft ceiling.

    The ceiling only alerts Mesio (LLM cost vs. price); it never stops the bot.
    It used to read columns that do not exist (su.conversations_used,
    pl.conversations_per_month), so it always failed into [] and never alerted.
    """
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    o.id AS org_id,
                    o.name AS org_name,
                    o.plan_code,
                    o.current_period_convs_used AS conversations_used,
                    pl.conv_cap AS conversations_per_month
                FROM organizations o
                JOIN plan_limits pl ON pl.plan_code = o.plan_code
                WHERE pl.conv_cap > 0
                  AND o.current_period_convs_used >= pl.conv_cap * 0.9
                ORDER BY
                    (o.current_period_convs_used::float / pl.conv_cap) DESC
                """
            )

        results = []
        for row in rows:
            org_id = row["org_id"]
            org_name = row["org_name"] or f"Org #{org_id}"
            used = int(row["conversations_used"] or 0)
            limit = int(row["conversations_per_month"] or 1)
            pct = round(used / limit * 100, 1)
            at_cap = used >= limit
            severity = "high" if at_cap else "medium"
            results.append(
                {
                    "id": f"plan_cap:{org_id}",
                    "type": "plan_cap",
                    "severity": severity,
                    "title": f"{org_name} {'alcanzó' if at_cap else 'está cerca de'} su límite del plan",
                    "detail": (
                        f"Org #{org_id} ({row['plan_code']}): "
                        f"{used:,}/{limit:,} conversaciones ({pct}% del plan mensual)."
                    ),
                    "url": f"/internal/org/{org_id}",
                    "created_at": _now_iso(),
                    "count": 1,
                    "tenant_id": org_id,
                }
            )
        return results
    except Exception:
        log.exception("notifications_repo.plan_cap_error")
        return []


# ── Aggregator ────────────────────────────────────────────────────────────────

_HQ_ALERT_SEVERITY = {"critical": "critical", "warning": "high"}


async def _fetch_hq_alerts() -> list[dict]:
    """Open Mesio HQ alerts (migration 0109) — the restaurants' health flags
    the scheduler keeps in step. Each one links to the org's ficha."""
    try:
        from app.repositories.internal.hq_alerts_repo import db_list_alerts  # noqa: PLC0415
        rows = await db_list_alerts(status="open", org_id=None, limit=100)
    except Exception:
        log.exception("notifications_repo.hq_alerts_error")
        return []
    out = []
    for a in rows:
        org = a.get("org_name") or f"Org #{a['org_id']}"
        sede = a.get("location_name")
        if sede and sede.strip().lower() == org.strip().lower():
            sede = None  # one-sede orgs named like their sede: "Demo Mesio · Demo Mesio"
        out.append({
            "id": f"hq_alert:{a['id']}",
            "type": "hq_alert",
            "severity": _HQ_ALERT_SEVERITY.get(a["severity"], "medium"),
            "title": f"{org}{' · ' + sede if sede else ''}: {a['title']}",
            "detail": a.get("detail") or "",
            "url": f"/internal/org/{a['org_id']}",
            "created_at": a["opened_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if a.get("opened_at") else _now_iso(),
            "count": a.get("count") or 1,
            "tenant_id": a["org_id"],
        })
    return out


async def db_get_notifications() -> list[dict]:
    """
    Aggregate operational notifications from all sources.

    Cross-tenant — wrap caller in bypass_tenant_scope("internal_notifications_aggregate").

    Returns a list sorted by severity (critical > high > medium > low),
    then by created_at desc. Maximum 50 items.

    Each source is best-effort: if one raises, others still return.
    """
    import asyncio  # noqa: PLC0415

    results_per_source = await asyncio.gather(
        _fetch_cost_runaway(),
        _fetch_churn_risk(),
        _fetch_new_prospects(),
        _fetch_plan_cap_warnings(),
        _fetch_hq_alerts(),
        return_exceptions=False,  # each source already catches its own exceptions
    )

    combined: list[dict] = []
    for source_list in results_per_source:
        combined.extend(source_list)

    # Newest first, then (stable sort) by severity: within each severity the
    # most recent stays on top. The old code sorted twice by severity only,
    # so the order followed the source list and a fresh critical alert could
    # fall past the 50-item cap.
    combined.sort(key=lambda n: n.get("created_at", ""), reverse=True)
    combined.sort(key=lambda n: _SEVERITY_ORDER.get(n.get("severity", "low"), 99))

    return combined[:50]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
