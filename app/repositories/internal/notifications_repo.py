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

        today = date.today()
        pool = await _get_pool()
        async with pool.acquire() as conn:
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
                    "url": "/internal/costs",
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
    """Tenants with recent avg conversation volume < 50% of their baseline."""
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH daily AS (
                    SELECT
                        o.id AS org_id,
                        o.name AS org_name,
                        (c.created_at::date) AS day,
                        COUNT(*) AS cnt
                    FROM conversations c
                    JOIN organizations o ON o.id = c.org_id
                    WHERE c.created_at >= CURRENT_DATE - INTERVAL '21 days'
                    GROUP BY o.id, o.name, c.created_at::date
                ),
                baseline AS (
                    SELECT org_id, org_name,
                        AVG(cnt)::float AS baseline_avg
                    FROM daily
                    WHERE day < CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY org_id, org_name
                ),
                recent AS (
                    SELECT org_id,
                        AVG(cnt)::float AS recent_avg
                    FROM daily
                    WHERE day >= CURRENT_DATE - INTERVAL '7 days'
                    GROUP BY org_id
                )
                SELECT
                    b.org_id,
                    b.org_name,
                    b.baseline_avg,
                    COALESCE(r.recent_avg, 0) AS recent_avg
                FROM baseline b
                LEFT JOIN recent r ON r.org_id = b.org_id
                WHERE b.baseline_avg >= $1
                  AND COALESCE(r.recent_avg, 0) < $2 * b.baseline_avg
                ORDER BY (COALESCE(r.recent_avg, 0) / NULLIF(b.baseline_avg, 0)) ASC NULLS LAST
                """,
                _CHURN_BASELINE_MIN,
                _CHURN_RATIO_THRESHOLD,
            )

        results = []
        for row in rows:
            org_id = row["org_id"]
            org_name = row["org_name"] or f"Org #{org_id}"
            baseline = float(row["baseline_avg"])
            recent = float(row["recent_avg"])
            ratio = recent / baseline if baseline > 0 else 0.0
            drop_pct = round((1.0 - ratio) * 100, 1)
            results.append(
                {
                    "id": f"churn:{org_id}",
                    "type": "churn",
                    "severity": "medium",
                    "title": f"{org_name} muestra señal de churn",
                    "detail": (
                        f"Baseline 14d = {baseline:.1f} conv/día, "
                        f"últimos 7d = {recent:.1f} conv/día. "
                        f"Caída del {drop_pct}%."
                    ),
                    "url": "/internal/superadmin",
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


async def _fetch_suspended_tenants() -> list[dict]:
    """Organizations with subscription_status = 'suspended'."""
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name FROM organizations WHERE subscription_status = 'suspended' ORDER BY id"
            )

        results = []
        for row in rows:
            org_id = row["id"]
            org_name = row["name"] or f"Org #{org_id}"
            results.append(
                {
                    "id": f"suspended:{org_id}",
                    "type": "suspended",
                    "severity": "medium",
                    "title": f"{org_name} está suspendido",
                    "detail": f"Org #{org_id} tiene subscription_status = 'suspended'. Acción requerida.",
                    "url": f"/internal/superadmin",
                    "created_at": _now_iso(),
                    "count": 1,
                    "tenant_id": org_id,
                }
            )
        return results
    except Exception:
        log.exception("notifications_repo.suspended_tenants_error")
        return []


_TRIAL_WARNING_DAYS = 3


async def _fetch_billing_attention() -> list[dict]:
    """Orgs whose subscription needs Mesio this week (billing is manual):
    a trial ending within _TRIAL_WARNING_DAYS (call them before it pauses),
    a payment overdue in its grace days, and paused accounts."""
    from app.services import plans  # noqa: PLC0415

    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, comp_until, paid_until
                FROM organizations
                WHERE comp_until IS NOT NULL OR paid_until IS NOT NULL
                ORDER BY id
                """
            )

        now = datetime.now(tz=timezone.utc)
        results = []
        for row in rows:
            org_id = row["id"]
            org_name = row["name"] or f"Org #{org_id}"
            status = plans.billing_status(row["comp_until"], row["paid_until"], now)
            if status == plans.TRIAL:
                days = (row["comp_until"] - now).days
                if days >= _TRIAL_WARNING_DAYS:
                    continue
                kind, severity = "trial_ending", "medium"
                title = f"La prueba de {org_name} termina en {days + 1} día(s)"
                detail = "Escríbele para elegir plan: al terminar la prueba sin pago, la cuenta se pausa."
            elif status == plans.OVERDUE:
                kind, severity = "payment_overdue", "high"
                title = f"{org_name} tiene el pago vencido"
                detail = (f"Pagado hasta {row['paid_until'].date().isoformat()}. "
                          f"Se pausa {plans.PAYMENT_GRACE_DAYS} días después.")
            elif status == plans.SUSPENDED:
                kind, severity = "account_paused", "high"
                title = f"{org_name} está pausada"
                detail = "Sus clientes no pueden pedir por QR ni por su link hasta registrar un pago."
            else:
                continue
            results.append(
                {
                    "id": f"{kind}:{org_id}",
                    "type": kind,
                    "severity": severity,
                    "title": title,
                    "detail": detail,
                    "url": "/internal/superadmin",
                    "created_at": _now_iso(),
                    "count": 1,
                    "tenant_id": org_id,
                }
            )
        return results
    except Exception:
        log.exception("notifications_repo.billing_attention_error")
        return []


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
                    "url": "/internal/superadmin",
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
        _fetch_suspended_tenants(),
        _fetch_plan_cap_warnings(),
        _fetch_billing_attention(),
        return_exceptions=False,  # each source already catches its own exceptions
    )

    combined: list[dict] = []
    for source_list in results_per_source:
        combined.extend(source_list)

    # Sort: severity priority first, then created_at descending (stable)
    combined.sort(
        key=lambda n: (
            _SEVERITY_ORDER.get(n.get("severity", "low"), 99),
            # negate string sort by making it negative index — just use reverse on second key
            n.get("created_at", ""),
        )
    )
    # created_at is ISO string so lexicographic sort is chronological;
    # we want most recent first within the same severity — reverse the secondary key
    combined.sort(
        key=lambda n: (
            _SEVERITY_ORDER.get(n.get("severity", "low"), 99),
        )
    )

    return combined[:50]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
