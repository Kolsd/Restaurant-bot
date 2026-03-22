import hashlib
import os
import httpx   
import asyncio
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from app.services import database as db
from app.services.orders import cart_summary, clear_cart
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

router = APIRouter()

# Sin contraseñas por defecto por seguridad
WOMPI_EVENTS_SECRET = os.getenv("WOMPI_EVENTS_SECRET")

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
    await clear_cart(request.phone, request.bot_number)
    return {"success": True}

@router.get("/cart/{phone}/{bot_number}")
async def view_cart(phone: str, bot_number: str):
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

class UpdateOrderStatusRequest(BaseModel):
    status: str

@router.get("/api/delivery/orders")
async def get_delivery_orders():
    # Traemos los pedidos que el domiciliario necesita ver
    orders = await db.db_get_delivery_orders(['listo', 'en_camino', 'entregado'])
    return {"orders": orders}

# --- FUNCIONES Y ENDPOINTS DEL DOMICILIARIO ---

class UpdateOrderStatusRequest(BaseModel):
    status: str

async def send_delivery_notification(phone: str, status: str):
    """Envía un mensaje automático de WhatsApp según el estado del pedido"""
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    phone_id = os.getenv("META_PHONE_NUMBER_ID", "") 
    
    if not token or not phone_id:
        print("⚠️ No hay credenciales de Meta para enviar la notificación.")
        return 
        
    if status == 'en_camino':
        msg = "🛵 *¡Buenas noticias!*\n\nNuestro domiciliario acaba de salir del restaurante con tu pedido. ¡Ve preparando la mesa! 🍔"
    elif status == 'entregado':
        msg = "✅ *¡Pedido Entregado!*\n\nEsperamos que lo disfrutes muchísimo. ¡Gracias por elegirnos y buen provecho! 🌟"
    else:
        return # No enviamos mensajes para otros estados

    clean_phone = phone.replace("+", "").replace(" ", "")
    
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"https://graph.facebook.com/v20.0/{phone_id}/messages",
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


@router.get("/api/delivery/check-updates")
async def check_delivery_updates():
    """Consulta ultra-ligera para saber si hay cambios en los pedidos del domiciliario"""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, status 
            FROM orders 
            WHERE order_type='delivery' AND status IN ('listo', 'en_camino') 
            ORDER BY id
        """)
        current_state_hash = "".join([f"{r['id']}{r['status']}" for r in rows])
        return {"hash": current_state_hash}


@router.get("/api/delivery/orders")
async def get_delivery_orders():
    orders = await db.db_get_delivery_orders(['listo', 'en_camino', 'entregado'])
    return {"orders": orders}


@router.patch("/api/delivery/orders/{order_id}/status")
async def update_delivery_status(order_id: str, req: UpdateOrderStatusRequest):
    # 1. Buscamos el pedido original en la base de datos para obtener el número del cliente
    order = await db.db_get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Orden no encontrada")
        
    # 2. Actualizamos el estado
    await db.db_update_order_status(order_id, req.status)
    
    # 3. Disparamos el mensaje de WhatsApp en SEGUNDO PLANO
    # Solo si el estado es 'en_camino' o 'entregado'
    if req.status in ['en_camino', 'entregado']:
        asyncio.create_task(send_delivery_notification(order["phone"], req.status))
        
    return {"success": True, "new_status": req.status}