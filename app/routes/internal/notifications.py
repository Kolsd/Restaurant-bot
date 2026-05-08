"""
app/routes/internal/notifications.py

Notifications endpoint for the Mesio HQ Action Queue.

GET /api/internal/notifications
  Returns aggregated operational notifications sorted by priority.
  Requires superadmin session token.

Response shape:
  {
    "items": [...],
    "count_by_severity": {"critical": 0, "high": 2, "medium": 1, "low": 0}
  }
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.routes.deps import verify_superadmin
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal-notifications"])


def _count_by_severity(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for item in items:
        sev = item.get("severity", "low")
        if sev in counts:
            counts[sev] += 1
    return counts


@router.get("/notifications")
async def get_notifications(_: None = Depends(verify_superadmin)):
    """Return aggregated operational notifications for the HQ Action Queue.

    Cross-tenant: runs inside bypass_tenant_scope so all source queries
    can access data across all organizations.
    """
    from app.repositories.internal import notifications_repo  # noqa: PLC0415

    with bypass_tenant_scope("internal_notifications_aggregate"):
        items = await notifications_repo.db_get_notifications()

    return {
        "items": items,
        "count_by_severity": _count_by_severity(items),
    }
