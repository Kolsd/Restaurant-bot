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

from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

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
