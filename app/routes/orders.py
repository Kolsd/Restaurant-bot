import hashlib
import os
import httpx
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.services.orders import cart_summary, clear_cart
from app.routes.deps import require_auth
from app.services.agent import trigger_nps

META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

router = APIRouter()

# Sin contraseñas por defecto por seguridad
WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET")

class ClearCartRequest(BaseModel):
    phone: str
    bot_number: str


@router.get("/orders")
async def list_orders(request: Request):
    await require_auth(request)
    all_orders = await db.db_get_all_orders()
    paid = [o for o in all_orders if o["paid"]]
    total_revenue = sum(o["total"] for o in paid)
    return {
        "summary": {
            "total_orders": len(all_orders),
            "paid": len(paid),
            "pending_payment": len(all_orders) - len(paid),
            "total_revenue": total_revenue,
        },
        "orders": all_orders,
    }


@router.get("/orders/{order_id}")
async def get_single_order(request: Request, order_id: str):
    await require_auth(request)
    order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order

@router.post("/cart/clear")
async def clear_user_cart(request: ClearCartRequest):
    await clear_cart(request.phone, request.bot_number)
    return {"success": True}

@router.get("/cart/{phone}/{bot_number}")
async def view_cart(request: Request, phone: str, bot_number: str):
    await require_auth(request)
    summary = await cart_summary(phone, bot_number)
    return {"summary": summary}

@router.post("/payment/wompi-webhook")
async def wompi_webhook(request: Request):
    # 🚨 FIX DE SEGURIDAD CON INDENTACIÓN CORRECTA
    if not WOMPI_EVENTS_SECRET:
        print("🚨 ALERTA: Intento de webhook de Wompi, pero WOMPI_EVENTS_SECRET no está configurado.", flush=True)
        raise HTTPException(status_code=500, detail="Configuración de pasarela de pagos incompleta")

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
                    print(f"Payment confirmed — Order: {reference} — {result['total']}", flush=True)

    return {"status": "ok"}

@router.get("/payment/confirm")
async def payment_confirm(request: Request):
    params = dict(request.query_params)
    order_id = params.get("id", "")
    status = params.get("status", "")
    order = await db.db_get_order(order_id) if order_id else None

    if status == "APPROVED" and order:
        return {
            "message": "Payment successful",
            "order_id": order_id,
            "total": order['total'],
            "status": "Your order is being prepared"
        }
    return {
        "message": "Payment not completed",
        "order_id": order_id,
        "status": status
    }

class UpdateOrderStatusRequest(BaseModel):
    status: str

# --- FUNCIONES Y ENDPOINTS DEL DOMICILIARIO ---

class UpdateOrderStatusRequest(BaseModel):
    status: str

async def send_delivery_notification(phone: str, status: str, bot_number: str = ""):
    """Envía un mensaje automático de WhatsApp según el estado del pedido"""
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("META_PHONE_NUMBER_ID", "")

    if not token or not phone_id:
        print("⚠️ No hay credenciales de Meta para enviar la notificación.")
        return

    if status == 'en_camino':
        msg = "🛵 *¡Buenas noticias!*\n\nNuestro domiciliario acaba de salir del restaurante con tu pedido. ¡Ve preparando la mesa! 🍔"
    elif status == 'en_puerta':
        msg = "📍 *¡El domiciliario está en la puerta!*\n\n¡Ya casi llega tu pedido! Por favor ten listo el pago si aplica. 🏠"
    elif status == 'entregado':
        rest_name = ""
        if bot_number:
            try:
                rest = await db.db_get_restaurant_by_bot_number(bot_number)
                if rest:
                    rest_name = rest.get("name", "")
            except Exception:
                pass
        nps_label = rest_name or "nuestro restaurante"
        msg = (
            "✅ *¡Pedido Entregado!*\n\nEsperamos que lo disfrutes muchísimo. ¡Gracias por elegirnos y buen provecho! 🌟"
            f"\n\n⭐ ¿Cómo calificarías tu experiencia con *{nps_label}*?\n"
            "Responde con un número del *1 al 5*\n"
            "_(1 = Muy mala · 5 = Excelente)_"
        )
    else:
        return # No enviamos mensajes para otros estados

    clean_phone = phone.replace("+", "").replace(" ", "")

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": clean_phone,
                    "type": "text",
                    "text": {"body": msg}
                }
            )
            print(f"📤 Notificación de delivery enviada a {clean_phone}")
    except Exception as e:
        print(f"❌ Error enviando notificación de delivery: {e}")

    # After entregado notification: set NPS state and clear conversation
    if status == 'entregado' and bot_number:
        try:
            await trigger_nps(phone, bot_number, rest_name)
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM conversations WHERE phone=$1 AND bot_number=$2",
                    phone, bot_number
                )
            print(f"✅ Sesión limpiada post-entrega: {phone}", flush=True)
        except Exception as e:
            print(f"❌ Error en cleanup post-entrega: {e}", flush=True)


@router.get("/delivery/check-updates")
async def check_delivery_updates(request: Request):
    await require_auth(request)

    """Consulta ultra-ligera para saber si hay cambios en los pedidos del domiciliario"""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, status
            FROM orders
            WHERE order_type IN ('domicilio', 'recoger') AND status IN ('listo', 'en_camino', 'en_puerta')
            ORDER BY id
        """)
        current_state_hash = "".join([f"{r['id']}{r['status']}" for r in rows])
        return {"hash": current_state_hash}


@router.get("/delivery/orders")
async def get_delivery_orders(request: Request):
    await require_auth(request)

    orders = await db.db_get_delivery_orders(['listo', 'en_camino', 'en_puerta', 'entregado'])
    return {"orders": orders}


@router.patch("/delivery/orders/{order_id}/status")
async def update_delivery_status(order_id: str, req: UpdateOrderStatusRequest, request: Request):
    await require_auth(request)

    # 1. Buscamos el pedido original en la base de datos para obtener el número del cliente
    order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
        
    # 2. Actualizamos el estado
    await db.db_update_order_status(order_id, req.status)
    
    # 3. Disparamos el mensaje de WhatsApp en SEGUNDO PLANO
    if req.status in ['en_camino', 'en_puerta', 'entregado']:
        asyncio.create_task(send_delivery_notification(order["phone"], req.status, order.get("bot_number", "")))

    return {"success": True, "new_status": req.status}