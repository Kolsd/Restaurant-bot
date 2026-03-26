"""
POST /api/sync — Offline-first batch synchronization endpoint.

Accepts an array of operations generated offline by the browser (offline-sync.js).
Each operation carries a client-generated UUID as its primary key, enabling
safe upserts (INSERT ... ON CONFLICT (id) DO UPDATE) without server-side
collision risk.

Protected by standard Bearer token auth via get_current_restaurant.
"""
from typing import Any
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from app.routes.deps import get_current_restaurant
from app.services import database as db

router = APIRouter()


class SyncOperation(BaseModel):
    id: str                        # Client-generated UUID v4
    type: str                      # Entity type: 'staff_shift', 'staff', etc.
    action: str = "upsert"         # Always 'upsert' for offline-first
    data: dict[str, Any]           # The record payload
    client_ts: str | None = None   # ISO-8601 timestamp from the client


class SyncBatch(BaseModel):
    operations: list[SyncOperation]


@router.post("/sync")
async def sync_batch(
    batch: SyncBatch,
    restaurant: dict = Depends(get_current_restaurant),
):
    """
    Process a batch of offline operations.

    Returns:
        {
            "synced": <count of successful upserts>,
            "errors": [{"id": "...", "error": "..."}],
            "total":  <total operations received>
        }
    """
    results = await db.db_sync_batch(
        restaurant_id=restaurant["id"],
        operations=[op.model_dump() for op in batch.operations],
    )
    synced = sum(1 for r in results if r["status"] == "ok")
    errors = [r for r in results if r["status"] != "ok"]
    return {"synced": synced, "errors": errors, "total": len(batch.operations)}
