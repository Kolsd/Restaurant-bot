import hashlib
import os
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services.orders import (
    get_all_orders, get_order, confirm_payment,
    get_orders_by_phone, cart_summary, clear_cart
)

router = APIRouter()

WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET", "TU_SECRETO_EVENTOS")


# ─────────────────────────────────────────────
# MODELOS
# ─────────────────────────────────────────────

class ClearCartRequest(BaseModel):
    phone: str


# ─────────────────────────────────────────────
# ADMIN — Ver órdenes
# ─────────────────────────────────────────────

@router.get("/orders")
async def list_orders():
    """Ver todas las órdenes del restaurante."""
    all_orders = get_all_orders()
    paid = [o for o in all_orders if o["paid"]]
    pending = [o for o in all_orders if not o["paid"]]
    total_revenue = sum(o["total"] for o in paid)
    return {
        "summary": {
            "total_orders": len(all_orders),
            "paid": len(paid),
            "pending_payment": len(pending),
            "total_revenue_cop": total_revenue
        },
        "orders": sorted(all_orders, key=lambda x: x["created_at"], reverse=True)
    }


@router.get("/orders/{order_id}")
async def get_single_order(order_id: str):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
    return order


@router.get("/orders/phone/{phone}")
async def orders_by_phone(phone: str):
    return {"orders": get_orders_by_phone(phone)}


@router.post("/cart/clear")
async def clear_user_cart(request: ClearCartRequest):
    clear_cart(request.phone)
    return {"success": True}


@router.get("/cart/{phone}")
async def view_cart(phone: str):
    summary = cart_summary(phone)
    return {"summary": summary}


# ─────────────────────────────────────────────
# WOMPI WEBHOOK — Confirmación de pago
# ─────────────────────────────────────────────

@router.post("/payment/wompi-webhook")
async def wompi_webhook(request: Request):
    """
    Recibe notificaciones de pago de Wompi.
    Configura este URL en tu dashboard de Wompi → Desarrolladores → Eventos.
    """
    body = await request.json()

    # Verificar firma del webhook (seguridad)
    signature_header = request.headers.get("x-event-checksum", "")
    body_str = await request.body()
    expected_sig = hashlib.sha256(
        (body_str.decode() + WOMPI_EVENTS_SECRET).encode()
    ).hexdigest()

    if signature_header and signature_header != expected_sig:
        raise HTTPException(status_code=401, detail="Firma inválida")

    event = body.get("event", "")
    data = body.get("data", {})

    if event == "transaction.updated":
        transaction = data.get("transaction", {})
        status = transaction.get("status")
        reference = transaction.get("reference")
        transaction_id = transaction.get("id")

        if status == "APPROVED" and reference:
            result = confirm_payment(reference, transaction_id)
            if result["success"]:
                order = result["order"]
                print(f"✅ PAGO CONFIRMADO - Orden: {reference} - Total: ${order['total']:,} COP")
                # Aquí puedes enviar WhatsApp de confirmación al cliente via Twilio
                # y notificar al restaurante
            return {"status": "processed"}

    return {"status": "ignored"}


# ─────────────────────────────────────────────
# REDIRECT después del pago (Wompi redirect-url)
# ─────────────────────────────────────────────

@router.get("/payment/confirm")
async def payment_confirm(request: Request):
    """
    Wompi redirige al cliente aquí después de pagar.
    Muestra confirmación o error.
    """
    params = dict(request.query_params)
    order_id = params.get("id", "")
    status = params.get("status", "")

    order = get_order(order_id) if order_id else None

    if status == "APPROVED" and order:
        return {
            "message": "✅ ¡Pago exitoso!",
            "order_id": order_id,
            "total": f"${order['total']:,} COP",
            "status": "Tu pedido está siendo preparado 🍽️",
            "estimated_time": "45-60 min domicilio / 20-30 min recoger"
        }

    return {
        "message": "❌ Pago no completado",
        "order_id": order_id,
        "status": status,
        "help": "Si tuviste problemas, escríbenos al WhatsApp del restaurante"
    }
