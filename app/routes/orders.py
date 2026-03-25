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
    # Fetch restaurant credentials first (restaurant-specific phone_id takes priority)
    rest_name = ""
    rest_phone_id = ""
    rest_token = ""
    if bot_number:
        try:
            rest = await db.db_get_restaurant_by_bot_number(bot_number)
            if rest:
                rest_name = rest.get("name", "")
                rest_phone_id = rest.get("wa_phone_id", "") or ""
                rest_token = rest.get("wa_access_token", "") or ""
        except Exception:
            pass

    token = rest_token or os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    phone_id = rest_phone_id or os.getenv("META_PHONE_NUMBER_ID", "")

    if not token or not phone_id:
        print("⚠️ No hay credenciales de Meta para enviar la notificación.")
        return

    if status == 'en_camino':
        msg = "🛵 *¡Buenas noticias!*\n\nNuestro domiciliario acaba de salir del restaurante con tu pedido. ¡Ve preparando la mesa! 🍔"
    elif status == 'en_puerta':
        msg = "📍 *¡El domiciliario está en la puerta!*\n\n¡Ya casi llega tu pedido! Por favor ten listo el pago si aplica. 🏠"
    elif status == 'entregado':
        msg = "✅ *¡Pedido Entregado!*\n\nEsperamos que lo disfrutes muchísimo. ¡Gracias por elegirnos y buen provecho! 🌟"
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

    # After entregado: trigger NPS and send interactive question with skip button
    if status == 'entregado' and bot_number:
        try:
            await trigger_nps(phone, bot_number, rest_name)
            print(f"⭐ NPS iniciado post-entrega: {phone}", flush=True)
        except Exception as e:
            print(f"❌ Error iniciando NPS post-entrega: {e}", flush=True)

        # Send interactive NPS message with "No calificar" button
        try:
            nps_label = rest_name or "nuestro restaurante"
            nps_text = (
                f"⭐ ¿Cómo calificarías tu experiencia con {nps_label}?\n"
                "Responde con un número del 1 al 5\n"
                "(1 = Muy mala · 5 = Excelente)"
            )
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": clean_phone,
                        "type": "interactive",
                        "interactive": {
                            "type": "button",
                            "body": {"text": nps_text},
                            "action": {
                                "buttons": [
                                    {"type": "reply", "reply": {"id": "skip_nps", "title": "No calificar"}}
                                ]
                            }
                        }
                    }
                )
        except Exception as e:
            print(f"❌ Error enviando NPS interactivo delivery: {e}")


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

    raw = await db.db_get_delivery_orders(['listo', 'en_camino', 'en_puerta', 'entregado'])

    # Merge sub-orders into their base order so the domiciliario sees one card per delivery
    groups: dict = {}  # base_id -> merged order dict
    for o in raw:
        base_id = o.get("base_order_id") or o["id"]
        if base_id not in groups:
            groups[base_id] = dict(o)
            groups[base_id]["id"] = base_id  # always expose the base id
            groups[base_id]["sub_order_count"] = 1
        else:
            base = groups[base_id]
            # Merge items (sum quantities for same dish name)
            items_map = {i["name"]: dict(i) for i in base["items"]}
            for item in o.get("items", []):
                name = item["name"]
                if name in items_map:
                    items_map[name]["quantity"] = items_map[name].get("quantity", 1) + item.get("quantity", 1)
                    items_map[name]["subtotal"]  = items_map[name].get("subtotal", 0)  + item.get("subtotal", 0)
                else:
                    items_map[name] = dict(item)
            base["items"] = list(items_map.values())
            base["total"] = base.get("total", 0) + o.get("total", 0)
            base["sub_order_count"] = base.get("sub_order_count", 1) + 1

    return {"orders": list(groups.values())}


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