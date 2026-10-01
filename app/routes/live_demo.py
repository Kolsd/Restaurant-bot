"""/demo — the live demo page and its public API (app/services/live_demo.py).

Public and unauthenticated by design: every call is rate-limited per IP, and
everything it can read or change is scoped to the demo restaurant's tables.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.services import live_demo, state_store

router = APIRouter(tags=["live-demo"])

_PAGE = Path(__file__).resolve().parent.parent / "static" / "html" / "demo.html"


class AdvanceRequest(BaseModel):
    status: str


async def _limit(request: Request, bucket: str, max_requests: int) -> None:
    ip = request.client.host if request.client else "unknown"
    if not await state_store.rate_limit_check(f"live_demo_{bucket}:{ip}", max_requests=max_requests,
                                              window_seconds=60):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Intenta en un momento.")


@router.get("/demo", response_class=HTMLResponse)
async def demo_page():
    return _PAGE.read_text(encoding="utf-8")


@router.post("/api/demo/table")
async def demo_table(request: Request):
    """A table of Casa Mesio for this visitor, with the link its QR opens."""
    await _limit(request, "table", 10)
    return await live_demo.assign_table()


@router.get("/api/demo/kitchen/{table_id}")
async def demo_kitchen(table_id: str, request: Request):
    """What the kitchen screen shows for this visitor's table."""
    await _limit(request, "kitchen", 90)
    if not live_demo.is_demo_table(table_id):
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    return {"orders": await live_demo.kitchen_orders(table_id)}


@router.post("/api/demo/kitchen/orders/{order_id}")
async def demo_advance(order_id: str, body: AdvanceRequest, request: Request):
    """Mark a demo ticket as the kitchen would; the visitor's phone hears it."""
    await _limit(request, "advance", 30)
    if body.status not in live_demo.KITCHEN_STATUSES:
        raise HTTPException(status_code=422, detail="Estado inválido")
    if not await live_demo.advance_order(order_id, body.status):
        raise HTTPException(status_code=404, detail="Pedido no encontrado")
    return {"ok": True, "status": body.status}
