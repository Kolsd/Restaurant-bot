"""
Mesio HQ — organization "ficha": the control-center view of one customer.

  GET /internal/org/{org_id}           → page (data loads from the API)
  GET /api/internal/hq/orgs/{org_id}   → snapshot: business, people, health
                                          flags with runbook, every sede

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
