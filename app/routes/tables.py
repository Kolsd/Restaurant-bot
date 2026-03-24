import os
import httpx
import urllib.parse
import uuid
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from app.services import database as db
from app.services.auth import verify_token
from app.services import billing
from app.services.agent import trigger_nps

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"

async def require_auth(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not await verify_token(token):
        raise HTTPException(status_code=401, detail="No autorizado")

async def get_table_wa_number(table: dict) -> str:
    wa_number = "15556293573"
    if table.get("branch_id"):
        r = await db.db_get_restaurant_by_id(table["branch_id"])
        if r:
            wa_number = r.get("whatsapp_number", wa_number)
    else:
        all_r = await db.db_get_all_restaurants()
        if all_r:
            wa_number = all_r[0].get("whatsapp_number", wa_number)
    return wa_number

class TableRequest(BaseModel):
    number: int
    name: str = ""
    branch_id: int = None

@router.get("/api/tables")
async def get_tables(request: Request):
    await require_auth(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user = await db.db_get_user(username) if username else None
    return {"tables": await db.db_get_tables(branch_id=user.get("branch_id") if user else None)}

@router.post("/api/tables")
async def create_table(request: Request, body: TableRequest):
    await require_auth(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user = await db.db_get_user(username) if username else None
    branch_id = body.branch_id or (user.get("branch_id") if user else None)
    table_id = f"{f'b{branch_id}-' if branch_id else ''}mesa-{body.number}"
    name = body.name or f"Mesa {body.number}"
    await db.db_create_table(table_id, body.number, name, branch_id=branch_id)
    return {"success": True, "table_id": table_id, "name": name}

@router.delete("/api/tables/{table_id}")
async def delete_table(request: Request, table_id: str):
    await require_auth(request)
    await db.db_delete_table(table_id)
    return {"success": True}

@router.get("/menu/{table_id}", response_class=HTMLResponse)
async def menu_page(table_id: str):
    p = STATIC / "menu.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="menu.html no encontrado en static/")
    return HTMLResponse(p.read_text(encoding="utf-8"))

@router.get("/api/public/menu-context/{table_id}")
async def public_menu_context(table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")

    wa_number = await get_table_wa_number(table)
    wa_msg = f"Hola! Estoy en {table['name']} y quiero hacer un pedido"
    wa_url = f"https://wa.me/{wa_number}?text={urllib.parse.quote(wa_msg)}"
    
    menu = await db.db_get_menu(wa_number) or {}
    availability = await db.db_get_menu_availability()

    return {
        "table_name": table["name"],
        "wa_url": wa_url,
        "menu": menu,
        "availability": availability
    }

def build_qr_html(menu_url: str, table_name: str, width: int = 300) -> str:
    return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body style='margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;'><div id='qr'></div><script>window.onload=function(){{new QRCode(document.getElementById('qr'),{{text:decodeURIComponent('{urllib.parse.quote(menu_url)}'),width:{width},height:{width},colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});}};</script></body></html>"

@router.get("/api/tables/{table_id}/qr", response_class=HTMLResponse)
async def get_table_qr(request: Request, table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    base_url = str(request.base_url).rstrip('/')
    menu_url = f"{base_url}/menu/{table_id}"
    return build_qr_html(menu_url, table["name"], width=300)

@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(request: Request, table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    base_url = str(request.base_url).rstrip('/')
    menu_url = f"{base_url}/menu/{table_id}"
    encoded = urllib.parse.quote(menu_url)
    return HTMLResponse(
        f"<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'><style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{font-family:Arial,sans-serif;background:#fff;}}.page{{width:10cm;margin:1cm auto;text-align:center;padding:1.5cm;border:2px solid #0D1412;border-radius:16px;}}.logo{{font-size:28px;font-weight:900;color:#0D1412;margin-bottom:4px;}}.logo span{{color:#1D9E75;}}.tname{{font-size:20px;font-weight:700;color:#0D1412;margin:12px 0 4px;}}.instr{{font-size:13px;color:#666;margin-bottom:16px;line-height:1.5;}}.qrbox{{width:200px;height:200px;margin:0 auto 16px;}}.qrbox canvas,.qrbox img{{width:200px !important;height:200px !important;border-radius:8px;}}.wa-badge{{display:inline-flex;align-items:center;gap:6px;background:#25D366;color:white;padding:8px 16px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:16px;}}.steps{{text-align:left;background:#f8f8f5;border-radius:10px;padding:12px 16px;margin-top:8px;}}.step{{font-size:12px;color:#444;padding:3px 0;display:flex;gap:8px;}}.sn{{color:#1D9E75;font-weight:700;}}@media print{{body{{margin:0;}}}}</style><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body><div class='page'><div class='logo'>Mesio<span>.</span></div><div class='tname'>{table['name']}</div><div class='instr'>Escanea el QR para ver el menú<br>y pedir por WhatsApp</div><div class='qrbox' id='qrc'></div><div class='wa-badge'>Ver Menú y Pedir</div><div class='steps'><div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div><div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div><div class='step'><span class='sn'>3.</span><span>Revisa el menú interactivo</span></div><div class='step'><span class='sn'>4.</span><span>Toca pedir por WhatsApp</span></div></div></div><script>window.onload=function(){{new QRCode(document.getElementById('qrc'),{{text:decodeURIComponent('{encoded}'),width:200,height:200,colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});setTimeout(function(){{window.print();}},800);}};</script></body></html>"
    )

# ── ALERTAS MESERO ──────────────────────────────────────────────────
@router.get("/api/waiter-alerts")
async def get_waiter_alerts(request: Request):
    await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            # Quitamos status=active para prevenir error 500
            rows = await conn.fetch("SELECT * FROM waiter_alerts ORDER BY created_at DESC LIMIT 30")
        except Exception as e:
            print(f"Error leyendo alertas: {e}")
            rows = []
    return {"alerts": [dict(r) for r in rows]}

@router.post("/api/waiter-alerts/{alert_id}/dismiss")
async def dismiss_waiter_alert(request: Request, alert_id: int):
    await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("DELETE FROM waiter_alerts WHERE id = $1", alert_id)
        except Exception:
            pass
    return {"success": True}

# ── ELIMINAR CONVERSACIONES (MANUAL) ─────────────────────────────────
@router.delete("/api/conversations/{phone}")
async def force_delete_conversation(request: Request, phone: str):
    """Permite al mesero limpiar un chat manualmente (ej. pruebas atascadas)"""
    await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("DELETE FROM conversations WHERE phone = $1", phone)
            await conn.execute("DELETE FROM carts WHERE phone = $1", phone)
            await conn.execute("UPDATE table_sessions SET status = 'closed', closed_at = NOW(), closed_by = 'manual_delete', closed_by_username = 'mesero' WHERE phone = $1 AND closed_at IS NULL", phone)
        except Exception as e:
            print(f"Error forzando limpieza de chat: {e}")
    return {"success": True}

# ── DELIVERY ORDERS ───────────────────────────────────────────────────
@router.get("/api/kitchen/delivery-orders")
async def get_delivery_orders(request: Request):
    await require_auth(request)
    import json as _json

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM orders 
               WHERE order_type IN ('domicilio','recoger') 
               AND created_at >= NOW() - INTERVAL '24 hours' 
               ORDER BY created_at DESC"""
        )

    orders = []
    for r in rows:
        items = r["items"]
        if isinstance(items, str):
            try: items = _json.loads(items)
            except: items = []
        orders.append({
            "id": r["id"],
            "phone": r["phone"],
            "items": items,
            "order_type": r["order_type"],
            "address": r.get("address", ""),
            "notes": r.get("notes", ""),
            "total": float(r["total"]),
            "paid": r.get("paid", False),
            "status": r.get("status", "confirmado"),
            "payment_method": r.get("payment_method", ""),
            "created_at": r["created_at"].isoformat() + "Z",
        })
    return {"orders": orders}

@router.get("/api/delivery/check-updates")
async def delivery_check_updates(request: Request):
    await require_auth(request)
    import hashlib as _hashlib
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, status FROM orders WHERE order_type IN ('domicilio','recoger') AND created_at >= NOW() - INTERVAL '24 hours' ORDER BY created_at DESC"
        )
    h = _hashlib.md5(str([(r["id"], r["status"]) for r in rows]).encode()).hexdigest()
    return {"hash": h}

@router.patch("/api/kitchen/delivery-orders/{order_id}/status")
async def update_delivery_order_status(request: Request, order_id: str):
    await require_auth(request)
    body = await request.json()
    new_status = body.get("status", "")
    valid = ["pendiente_pago", "confirmado", "en_preparacion", "listo", "en_camino", "entregado", "cancelado"]
    if new_status not in valid:
        raise HTTPException(status_code=400, detail="Estado inválido")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status=$2 WHERE id=$1", order_id, new_status)
        # Notificar al cliente si avanza
        if new_status in ("en_camino", "entregado"):
            row = await conn.fetchrow("SELECT phone, address, total FROM orders WHERE id=$1", order_id)
            if row:
                phone = row["phone"]
                if new_status == "en_camino":
                    msg = f"🛵 ¡Tu pedido ya va en camino a {row['address']}! Pronto estaremos contigo."
                else:
                    msg = f"✅ ¡Tu pedido fue entregado! Total: ${int(row['total']):,} COP. ¡Gracias por tu compra!"
                # Buscar phone_id de la sesión si existe
                try:
                    session = await conn.fetchrow(
                        "SELECT meta_phone_id, bot_number FROM table_sessions WHERE phone=$1 ORDER BY started_at DESC LIMIT 1",
                        phone
                    )
                    db_phone_id = session["meta_phone_id"] if session else None
                except Exception:
                    db_phone_id = None
                await send_wa_msg(phone, msg, db_phone_id)
    return {"success": True}

# ── TABLE ORDERS & OTHERS ──────────────────────────────────────────
@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None):
    await require_auth(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user = await db.db_get_user(username) if username else None
    branch_id = user.get("branch_id") if user else None
    
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if status:
            if branch_id:
                rows = await conn.fetch(
                    """SELECT o.* FROM table_orders o
                       LEFT JOIN restaurant_tables t ON o.table_id = t.id
                       WHERE o.status = $1 AND (t.branch_id = $2 OR t.branch_id IS NULL)
                       ORDER BY o.created_at ASC""", status, branch_id)
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status=$1 ORDER BY created_at ASC", status)
        else:
            if branch_id:
                rows = await conn.fetch(
                    """SELECT o.* FROM table_orders o
                       LEFT JOIN restaurant_tables t ON o.table_id = t.id
                       WHERE o.status NOT IN ('factura_entregada','cancelado')
                       AND (t.branch_id = $1 OR t.branch_id IS NULL)
                       ORDER BY o.created_at ASC""", branch_id)
            else:
                rows = await conn.fetch(
                    """SELECT * FROM table_orders 
                       WHERE status NOT IN ('factura_entregada','cancelado') 
                       ORDER BY created_at ASC""")

    import json as _json
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('items'), str):
            try: d['items'] = _json.loads(d['items'])
            except: pass
        if d.get('created_at') and hasattr(d['created_at'], 'isoformat'):
            d['created_at'] = d['created_at'].isoformat() + 'Z'
        if d.get('updated_at') and hasattr(d['updated_at'], 'isoformat'):
            d['updated_at'] = d['updated_at'].isoformat() + 'Z'
        result.append(d)
    return {"orders": result}

async def send_wa_msg(phone: str, text: str, db_phone_id: str = None):
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    final_phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_ID", "")
    
    if token and final_phone_id:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://graph.facebook.com/v20.0/{final_phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
                )
                print(f"✅ WA Notificación a {phone}: {resp.status_code}")
        except Exception as e:
            print(f"❌ Error enviando WhatsApp: {e}")
    else:
        print(f"⚠️ No se envió WA a {phone} -> Faltan variables (Token: {bool(token)}, PhoneID: {final_phone_id})")

@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    await require_auth(request)
    body = await request.json()
    status = body.get("status")
    
    # 1. Validar el estado actualizado
    valid_statuses = ['recibido', 'en_preparacion', 'listo', 'entregado', 'generar_factura', 'cerrar_mesa', 'factura_entregada', 'cancelado']
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Estado inválido")
    
    pool = await db.get_pool()
    
    async with pool.acquire() as conn:
        order_record = await conn.fetchrow("SELECT phone, table_name, base_order_id FROM table_orders WHERE id=$1", order_id)
        
    order = dict(order_record) if order_record else {}
    phone = order.get("phone")
    table_name = order.get("table_name", "tu mesa")
    
    db_phone_id = None
    session_data = None
    if phone:
        async with pool.acquire() as conn:
            try:
                session = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL", phone)
                if session:
                    session_data = dict(session)
                    db_phone_id = session_data.get("meta_phone_id")
            except Exception: pass

    # ── A. GENERAR FACTURA (Llama a Alegra/Siigo, NO cierra mesa) ──
    if status == "generar_factura":
        base_id = order.get("base_order_id") if order.get("base_order_id") else order_id
        bot_num = session_data.get("bot_number") if session_data else None
        
        if bot_num:
            try:
                rest = await db.db_get_restaurant_by_bot_number(bot_num)
                if rest:
                    await billing.emit_invoice(base_id, rest["id"])
            except Exception as e:
                print(f"❌ Error en la integración contable: {e}", flush=True)

        await db.db_update_table_order_status(base_id, "factura_generada")
        
        if phone:
            msg = "🧾 Estamos generando tu cuenta. En un momento el mesero te la entregará en la mesa."
            await send_wa_msg(phone, msg, db_phone_id)

        return {"success": True, "order_id": order_id, "status": "factura_generada"}

    # ── B. CERRAR MESA (Limpia BD, despide, NO emite factura) ──
    elif status in ("cerrar_mesa", "factura_entregada"):
        base_id = order.get("base_order_id") if order.get("base_order_id") else order_id
 
        await db.db_close_table_bill(base_id)  # status en BD = factura_entregada
 
        if phone:
            msg = "👋 Tu mesa ha sido cerrada. ¡Muchas gracias por visitarnos, esperamos verte de nuevo pronto!"
            await send_wa_msg(phone, msg, db_phone_id)
 
            # ── Resolver bot_number y restaurant_name para NPS ──
            bot_num = ""
            rest_name = ""
            if session_data and session_data.get("bot_number"):
                bot_num = session_data["bot_number"]
                try:
                    rest = await db.db_get_restaurant_by_bot_number(bot_num)
                    if rest:
                        rest_name = rest.get("name", "")
                except Exception:
                    pass
 
            # ── Disparar NPS ──
            if bot_num:
                await trigger_nps(phone, bot_num, rest_name)
 
            try:
                if session_data and session_data.get("bot_number"):
                    await db.db_close_session(phone, session_data["bot_number"], "factura_entregada", "mesero")
 
                async with pool.acquire() as conn:
                    # 👇 AQUÍ ESTÁ EL CAMBIO IMPORTANTÍSIMO: status = 'closed'
                    await conn.execute(
                        "UPDATE table_sessions SET status = 'closed', closed_at = NOW(), closed_by = 'factura_entregada', closed_by_username = 'mesero' WHERE phone = $1 AND closed_at IS NULL",
                        phone
                    )
                    await conn.execute("DELETE FROM conversations WHERE phone = $1", phone)
                    await conn.execute("DELETE FROM carts WHERE phone = $1", phone)
                print(f"🧹 CHAT, CARRITO E HISTORIAL BORRADOS DEFINITIVAMENTE PARA: {phone}")
            except Exception as e:
                print(f"Error limpiando BD tras cerrar mesa: {e}")
 
        return {"success": True, "order_id": order_id, "status": "factura_entregada"}
        
    # ── C. ESTADOS NORMALES (Prep, Listo, Entregado) ──
    else:
        await db.db_update_table_order_status(order_id, status)
        if status == "entregado" and phone:
            msg = f"🍽️ ¡Tu pedido ha sido entregado en la {table_name}!\n\nDisfruta tu comida. Cuando termines, puedes pedirme la cuenta por aquí mismo."
            await send_wa_msg(phone, msg, db_phone_id)

    return {"success": True, "order_id": order_id, "status": status}

@router.get("/cocina", response_class=HTMLResponse)
async def kitchen_display():
    return HTMLResponse((STATIC / "kitchen.html").read_text(encoding="utf-8"))

# ── MÓDULO PUNTO DE VENTA (POS) PARA MESEROS ─────────────────────────

class ManualOrderRequest(BaseModel):
    table_id: str
    table_name: str
    items: list
    total: int
    notes: str = ""

@router.get("/api/pos/menu")
async def get_pos_menu(request: Request):
    """Devuelve el menú del restaurante para pintarlo en el POS del mesero"""
    await require_auth(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user = await db.db_get_user(username)
    
    # Buscamos el número de WhatsApp asociado a la sucursal del mesero
    wa_number = "15556293573" # Fallback por defecto
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r: wa_number = r.get("whatsapp_number", wa_number)
    else:
        all_r = await db.db_get_all_restaurants()
        if all_r: wa_number = all_r[0].get("whatsapp_number", wa_number)
        
    menu = await db.db_get_menu(wa_number) or {}
    return {"menu": menu}

@router.get("/api/pos/tables-status")
async def get_tables_status(request: Request):
    """Devuelve todas las mesas y su estado actual (ideal para pintar el mapa)"""
    await require_auth(request)
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user = await db.db_get_user(username)
    branch_id = user.get("branch_id") if user else None
    
    tables = await db.db_get_tables(branch_id=branch_id)
    
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        # Buscamos qué mesas están hablando con el bot
        active_sessions = await conn.fetch("SELECT table_id FROM table_sessions WHERE status='active'")
        # Buscamos qué mesas tienen pedidos en curso
        pending_orders = await conn.fetch("SELECT table_id, status FROM table_orders WHERE status NOT IN ('factura_entregada', 'cancelado')")
        
    session_map = {s['table_id'] for s in active_sessions}
    order_map = {}
    for o in pending_orders:
        if o['table_id'] not in order_map:
            order_map[o['table_id']] = []
        order_map[o['table_id']].append(o['status'])
        
    # Armamos la respuesta consolidada para el frontend
    for t in tables:
        tid = t['id']
        t['bot_active'] = tid in session_map
        t['pending_orders'] = order_map.get(tid, [])
        
    return {"tables": tables}

@router.post("/api/pos/order")
async def pos_manual_order(request: Request, body: ManualOrderRequest):
    """Recibe la orden manual tocada en pantalla por el mesero y la manda a cocina"""
    await require_auth(request)
    
    # Generamos un ID único con prefijo 'pos-' para identificar que fue manual
    order_id = f"pos-{str(uuid.uuid4())[:8]}"
    phone = "manual" # Como es manual, no hay celular del cliente atado al inicio
    
    # 👇 LA MAGIA ESTÁ AQUÍ: Verificamos si la mesa ya tiene una orden activa
    base_id = await db.db_get_base_order_id(phone, body.table_id)
    
    if base_id:
        # Es una adición (sub-orden) a una cuenta que ya existe
        final_base_id = base_id
        sub_num = await db.db_get_next_sub_number(base_id)
    else:
        # Es el primer pedido de la mesa
        final_base_id = order_id
        sub_num = 1

    order = {
        "id": order_id,
        "table_id": body.table_id,
        "table_name": body.table_name,
        "phone": phone,
        "items": body.items,
        "status": "recibido", # Entra directamente a la cola de la cocina
        "notes": body.notes,
        "total": body.total,
        "base_order_id": final_base_id, 
        "sub_number": sub_num
    }
    
    await db.db_save_table_order(order)
    
    return {"success": True, "order_id": order_id, "message": "Comanda enviada a cocina"}