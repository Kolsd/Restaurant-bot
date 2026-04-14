"""
app/routes/legacy_redirects.py

301 redirects for internal tool URLs that were moved to /internal/* namespace.
These routes exist for 30 days to avoid breaking bookmarks used by the Mesio team.

Once logs show zero hits on internal.legacy_url events, these can be removed.

Legacy → New mappings:
  /api/crm/*              → /api/internal/crm/*
  /api/admin/*            → /api/internal/admin/*
  /api/analytics/*        → /api/internal/analytics/*
  /api/billing/admin/*    → /api/internal/billing/*
  /health/metrics         → /api/internal/ops/metrics
  /analytics              → /internal/analytics
  /monitoring             → /internal/monitoring
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["legacy-redirects"])


def _redirect(request: Request, new_path: str) -> RedirectResponse:
    log.info(
        "internal.legacy_url",
        path=str(request.url.path),
        redirect_to=new_path,
        method=request.method,
    )
    # Preserve query string
    qs = str(request.url.query)
    target = new_path + (f"?{qs}" if qs else "")
    return RedirectResponse(url=target, status_code=301)


# ── /api/crm/* ────────────────────────────────────────────────────────────────

@router.api_route("/api/crm/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def legacy_crm(request: Request, path: str):
    return _redirect(request, f"/api/internal/crm/{path}")


# ── /api/admin/* ─────────────────────────────────────────────────────────────

@router.api_route("/api/admin/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def legacy_admin(request: Request, path: str):
    return _redirect(request, f"/api/internal/admin/{path}")


# ── /api/analytics/* ─────────────────────────────────────────────────────────

@router.api_route("/api/analytics/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def legacy_analytics(request: Request, path: str):
    return _redirect(request, f"/api/internal/analytics/{path}")


# ── /api/billing/admin/* ─────────────────────────────────────────────────────

@router.api_route("/api/billing/admin/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def legacy_billing_admin(request: Request, path: str):
    return _redirect(request, f"/api/internal/billing/{path}")


# ── /health/metrics ──────────────────────────────────────────────────────────

@router.get("/health/metrics")
async def legacy_health_metrics(request: Request):
    return _redirect(request, "/api/internal/ops/metrics")


# ── /analytics (HTML page) ───────────────────────────────────────────────────

@router.get("/analytics")
async def legacy_analytics_page(request: Request):
    return _redirect(request, "/internal/analytics")


# ── /monitoring (HTML page) ──────────────────────────────────────────────────

@router.get("/monitoring")
async def legacy_monitoring_page(request: Request):
    return _redirect(request, "/internal/monitoring")
