"""
app/routes/internal/billing_admin.py

Billing admin endpoints for Mesio internal use only.
These are NOT restaurant-facing — they are maintenance/support tools.

Endpoints (under /api/internal/billing, require verify_superadmin):
  POST /config             → Set billing config for any restaurant by ID
  GET  /logs               → Read billing log for any restaurant by ID
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.routes.deps import verify_superadmin
from app.services.billing import save_billing_config, get_billing_log

router = APIRouter(prefix="/api/internal/billing", tags=["internal-billing"])


class AdminConfigPayload(BaseModel):
    admin_key:     str = ""
    restaurant_id: int
    config:        dict


@router.post("/config")
async def admin_set_config(payload: AdminConfigPayload, _: None = Depends(verify_superadmin)):
    await save_billing_config(payload.restaurant_id, payload.config)
    return {"success": True}


@router.get("/logs")
async def admin_logs(restaurant_id: int, limit: int = 100, _: None = Depends(verify_superadmin)):
    log = await get_billing_log(restaurant_id, limit)
    return {"log": log}
