"""
app/routes/internal/costs.py

Internal cost monitoring dashboard for the Mesio team.
All routes here require Authorization: Bearer <opaque-session-token>
validated via verify_superadmin (same pattern as analytics.py).

Endpoints:
  GET /internal/costs                                   → HTML dashboard page
  GET /api/internal/costs/summary?start=...&end=...     → platform totals
  GET /api/internal/costs/restaurants?start=...&end=... → per-restaurant ranking
  GET /api/internal/costs/restaurants/{org_id}?...      → single org detail
  GET /api/internal/costs/outliers?start=...&end=...    → runaway cost alerts
"""

import csv
import io
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response

from app.routes.deps import verify_superadmin
from app.services.tenant_context import bypass_tenant_scope
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["internal-costs"])


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/internal/costs")
async def costs_page():
    return FileResponse("app/static/html/internal/costs.html")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(val: str | None, fallback: date) -> date:
    if not val:
        return fallback
    try:
        return date.fromisoformat(val)
    except ValueError:
        return fallback


def _default_range():
    today = date.today()
    start = today.replace(day=1)  # month-to-date
    return start, today


# ── Summary ───────────────────────────────────────────────────────────────────

@router.get("/api/internal/costs/summary")
async def costs_summary(
    start: str | None = Query(default=None),
    end:   str | None = Query(default=None),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_platform_cost_summary

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        result = await db_platform_cost_summary(d_start, d_end)

    return result


# ── Per-restaurant ranking ────────────────────────────────────────────────────

@router.get("/api/internal/costs/restaurants")
async def costs_restaurants(
    start: str | None = Query(default=None),
    end:   str | None = Query(default=None),
    limit: int        = Query(default=50, ge=1, le=200),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_per_restaurant_costs

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        rows = await db_per_restaurant_costs(d_start, d_end, limit=limit)

    return {"restaurants": rows, "start": str(d_start), "end": str(d_end)}


# ── Single restaurant detail ──────────────────────────────────────────────────

@router.get("/api/internal/costs/restaurants/{org_id}")
async def costs_restaurant_detail(
    org_id: int,
    start: str | None = Query(default=None),
    end:   str | None = Query(default=None),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_restaurant_cost_detail

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        result = await db_restaurant_cost_detail(org_id, d_start, d_end)

    return result


# ── Platform margin ──────────────────────────────────────────────────────────

@router.get("/api/internal/costs/margin")
async def costs_margin(
    start: str | None = Query(default=None),
    end:   str | None = Query(default=None),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_platform_margin_summary

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        result = await db_platform_margin_summary(d_start, d_end)

    return result


# ── Per-restaurant CSV export ────────────────────────────────────────────────

@router.get("/api/internal/costs/per-restaurant.csv")
async def export_costs_csv(
    start: str | None = Query(default=None),
    end:   str | None = Query(default=None),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_per_restaurant_costs

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        rows = await db_per_restaurant_costs(d_start, d_end, limit=200)

    buf = io.StringIO()
    fieldnames = [
        "org_id", "org_name", "plan_code",
        "total_tokens", "estimated_cost_usd", "estimated_cost_cop",
        "margin_cop", "margin_pct", "orders_count",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=mesio_costs_{d_start}_{d_end}.csv"},
    )


# ── Cost drilldown (top 5 expensive days for a tenant) ───────────────────────

@router.get("/api/internal/costs/{org_id}/drilldown")
async def cost_drilldown(
    org_id: int,
    period: str = Query(default="mtd"),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_restaurant_cost_detail

    today = date.today()
    if period == "mtd":
        d_start = today.replace(day=1)
    elif period == "7d":
        d_start = today - timedelta(days=6)
    else:  # 30d
        d_start = today - timedelta(days=29)
    d_end = today

    with bypass_tenant_scope("internal_cost_drilldown"):
        detail = await db_restaurant_cost_detail(org_id, d_start, d_end)

    by_day = sorted(detail.get("by_day", []), key=lambda r: r.get("tokens", 0), reverse=True)[:5]
    totals = detail.get("totals", {})
    total_convs = totals.get("conversations_count") or totals.get("orders_count") or 0
    total_cost_usd = totals.get("cost_usd") or 0

    return {
        "org_id": org_id,
        "org_name": detail.get("org_name", ""),
        "plan_code": detail.get("plan_code", "free"),
        "period": period,
        "period_start": str(d_start),
        "period_end": str(d_end),
        "top5_days": by_day,
        "totals": totals,
        "conversations_count": total_convs,
        "cost_per_conversation": round(total_cost_usd / total_convs, 6) if total_convs else None,
        "margin_cop": detail.get("margin_cop"),
        "margin_pct": detail.get("margin_pct"),
    }


# ── Outliers ──────────────────────────────────────────────────────────────────

@router.get("/api/internal/costs/outliers")
async def costs_outliers(
    start:         str | None = Query(default=None),
    end:           str | None = Query(default=None),
    threshold_pct: int        = Query(default=200, ge=10, le=10000),
    _: None = Depends(verify_superadmin),
):
    from app.repositories.cost_metrics_repo import db_cost_outliers

    d_start, d_end = _default_range()
    d_start = _parse_date(start, d_start)
    d_end   = _parse_date(end,   d_end)

    with bypass_tenant_scope("internal_cost_endpoint"):
        outliers = await db_cost_outliers(d_start, d_end, threshold_pct=threshold_pct)

    return {
        "outliers": outliers,
        "threshold_pct": threshold_pct,
        "start": str(d_start),
        "end": str(d_end),
    }
