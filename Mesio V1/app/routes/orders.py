import hashlib
import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.services.orders import cart_summary, clear_cart

router = APIRouter()
WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET", "TU_SECRETO_EVENTOS")

class ClearCartRequest(BaseModel):
    phone: str
    bot_number: str

@router.get("/orders")
async def list_orders():
    all_orders = await db.db_get_all_orders()
    paid = [o for o in all_orders if o["paid"]]
    total_revenue = sum(o["total"] for o in paid)
    return {
        "summary": {
            "total_orders": len(all_orders),
            "paid": len(paid),
            "pending_payment": len(all_orders) - len(paid),
            "total_revenue_cop": total_revenue
        },
        "orders": all_orders
    }

@router.get("/orders/{order_id}")
async def get_single_order(order_id: str):
    order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return order

@router.post("/cart/clear")
async def clear_user_cart(request: ClearCartRequest):
    clear_cart(request.phone, request.bot_number)
    return {"success": True}

@router.get("/cart/{phone}/{bot_number}")
async def view_cart(phone: str, bot_number: str):
    return {"summary": cart_summary(phone, bot_number)}

@router.post("/payment/wompi-webhook")
async def wompi_webhook(request: Request):
    body = await request.json()
    body_bytes = await request.body()
    signature_header = request.headers.get("x-event-checksum", "")
    expected_sig = hashlib.sha256(
        (body_bytes.decode() + WOMPI_EVENTS_SECRET).encode()
    ).hexdigest()

    if signature_header and signature_header != expected_sig:
        raise HTTPException(status_code=401, detail="Firma inválida")

    event = body.get("event", "")
    data = body.get("data", {})

    if event == "transaction.updated":
        transaction = data.get("transaction", {})
        if transaction.get("status") == "APPROVED":
            reference = transaction.get("reference")
            transaction_id = transaction.get("id")
            if reference:
                result = await db.db_confirm_payment(reference, transaction_id)
                if result:
                    print(f"✅ PAGO CONFIRMADO — Orden: {reference} — ${result['total']:,} COP")

    return {"status": "ok"}

@router.get("/payment/confirm")
async def payment_confirm(request: Request):
    params = dict(request.query_params)
    order_id = params.get("id", "")
    status = params.get("status", "")
    order = await db.db_get_order(order_id) if order_id else None

    if status == "APPROVED" and order:
        return {
            "message": "✅ ¡Pago exitoso!",
            "order_id": order_id,
            "total": f"${order['total']:,} COP",
            "status": "Tu pedido está siendo preparado 🍽️"
        }
    return {
        "message": "❌ Pago no completado",
        "order_id": order_id,
        "status": status
    }