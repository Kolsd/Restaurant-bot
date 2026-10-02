"""
Mesio HQ — organization "ficha": the control-center view of one customer.

  GET /internal/org/{org_id}           → page (data loads from the API)
  GET /api/internal/hq/orgs/{org_id}   → snapshot: business, people, health
                                          flags with runbook, every sede
  GET /api/internal/hq/errors          → platform_errors by org (last N hours)
                                          + the newest groups across Mesio

Superadmin only. Read-only: support actions live in their own endpoints.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.repositories.cost_metrics_repo import db_restaurant_cost_detail
from app.routes.deps import verify_superadmin
from app.services import hq_snapshot
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(tags=["internal-hq"])


@router.get("/internal/org/{org_id}")
async def org_page(org_id: int):
    return FileResponse("app/static/html/internal/org.html")


@router.get("/api/internal/hq/orgs/{org_id}")
async def org_snapshot(org_id: int, _: None = Depends(verify_superadmin)):
    today = date.today()
    with bypass_tenant_scope("internal_hq: one organization's control-center snapshot"):
        cost = await db_restaurant_cost_detail(org_id, today - timedelta(days=30), today)
        snapshot = await hq_snapshot.build_org_snapshot(org_id, llm_cost=cost)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Organización no encontrada")
    return snapshot


@router.get("/api/internal/hq/errors")
async def platform_errors(hours: int = 24, _: None = Depends(verify_superadmin)):
    from app.repositories.internal import errors_repo  # noqa: PLC0415
    hours = max(1, min(hours, 24 * 30))
    with bypass_tenant_scope("internal_hq: platform error log across organizations"):
        by_org = await errors_repo.db_error_counts_by_org(hours=hours)
        groups = await errors_repo.db_error_groups(org_id=None, days=(hours + 23) // 24, limit=100)
    return {
        "hours": hours,
        "by_org": [
            {"org_id": r["org_id"], "org_name": r["org_name"] or ("Plataforma" if r["org_id"] is None else f"Org #{r['org_id']}"),
             "count": r["count"], "bot_count": r["bot_count"], "last_at": r["last_at"].isoformat() if r["last_at"] else None}
            for r in by_org
        ],
        "groups": [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in g.items()}
            for g in groups
        ],
    }
