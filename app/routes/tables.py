import asyncio
import os
import time
import httpx
import urllib.parse
import uuid
from decimal import Decimal
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from app.services import database as db
from app.services import billing
from app.services.agent import trigger_nps
from app.routes.deps import require_auth, get_current_user, get_current_restaurant
from app.services import loyalty as loyalty_svc
from app.services.money import to_decimal, money_mul, quantize_money

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"
META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")

# Rate-limit WA "entregado" notifications: max 1 per phone per 5 minutes
_entregado_notif_sent: dict = {}  # phone -> last sent timestamp (epoch seconds)
_ENTREGADO_COOLDOWN = 300  # seconds


def _can_send_entregado_notif(phone: str) -> bool:
    last = _entregado_notif_sent.get(phone, 0)
    if time.time() - last >= _ENTREGADO_COOLDOWN:
        _entregado_notif_sent[phone] = time.time()
        return True
    return False

async def get_table_wa_number(table: dict) -> str:
    wa_number = ""
    bid = table.get("branch_id")
    if bid:
        r = await db.db_get_restaurant_by_id(bid)
        if r: wa_number = r.get("whatsapp_number", "")
            
    if not wa_number:
        all_r = await db.db_get_all_restaurants()
        if all_r:
            matriz = next((res for res in all_r if not res.get("parent_restaurant_id")), all_r[0])
            wa_number = matriz.get("whatsapp_number", "")
        
    # 🛡️ Limpiamos el sufijo _b para que el enlace wa.me sea válido
    return wa_number.split("_b")[0] if wa_number else ""

async def _get_restaurant_for_table(table_id: str | None, session_data: dict | None) -> dict:
    """Resuelve el restaurante/sucursal a partir de la mesa o la sesión activa."""
    if table_id:
        table = await db.db_get_table_by_id(table_id)
        if table:
            bid = table.get("branch_id")
            if bid:
                r = await db.db_get_restaurant_by_id(bid)
                if r:
                    return r
    if session_data and session_data.get("bot_number"):
        r = await db.db_get_restaurant_by_bot_number(session_data["bot_number"])
        if r:
            return r
    all_r = await db.db_get_all_restaurants()
    return all_r[0] if all_r else {}

async def _farewell_and_nps(phone: str, table_id: str | None, session_data: dict | None, db_phone_id: str | None, username: str) -> None:
    rest = await _get_restaurant_for_table(table_id, session_data)
    # Usamos el bot_number limpio para que coincida con el webhook de Meta
    raw_bot_num = rest.get("whatsapp_number", "")
    clean_bot_num = raw_bot_num.split("_b")[0] if raw_bot_num else ""
    final_bot_num = (session_data.get("bot_number") if session_data else None) or clean_bot_num
    
    rest_name = rest.get("name", "nuestro restaurante")
    # Disparamos directamente la encuesta NPS
    if final_bot_num:
        asyncio.create_task(trigger_nps(phone, final_bot_num, rest_name))
        asyncio.create_task(send_wa_interactive_nps(phone, rest_name, db_phone_id))
        await db.db_mark_session_nps_pending(phone, final_bot_num)
        
    await db.db_cleanup_after_checkout(phone)

# ── MESAS ────────────────────────────────────────────────────────────

@router.get("/api/tables")
async def get_tables(request: Request):
    """Devuelve las mesas de la sucursal actual para pintarlas en el dashboard."""
    await require_auth(request)
    user = await get_current_user(request)
    
    # Por defecto, asumimos el branch_id del usuario (útil para meseros/gerentes)
    branch_id = user.get("branch_id")
    is_main = branch_id is None
    
    # 🛡️ Si el dueño usa el selector del Topbar:
    branch_header = request.headers.get("X-Branch-ID")
    if "owner" in user.get("role", "") or "admin" in user.get("role", ""):
        if branch_header and branch_header.isdigit():
            # Eligió una sucursal específica
            branch_id = int(branch_header)
            is_main = False
        else:
            # No hay header (eligió "Casa Matriz")
            branch_id = None
            is_main = True
        
    tables = await db.db_get_tables(branch_id=branch_id, is_main=is_main)
    return {"tables": tables}
    
@router.post("/api/tables")
async def create_table(request: Request):
    """Crea una mesa automáticamente sin pedir número ni nombre manual."""
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)
    
    restaurant_id = restaurant["id"]
    is_main = restaurant.get("parent_restaurant_id") is None
    
    # 🛡️ Forzamos la lectura del selector de sucursales igual que en el GET
    # Esto asegura que la mesa se cree donde el dueño está mirando
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and ("owner" in user.get("role", "") or "admin" in user.get("role", "")):
        branch_id = int(branch_header)
        branch_rest = await db.db_get_restaurant_by_id(branch_id)
        if branch_rest:
            restaurant_id = branch_id
            is_main = False
    
    # Llama a la creación automática que hicimos en database.py
    new_table = await db.db_auto_create_table(restaurant_id, is_main)
    
    return {"success": True, "table_id": new_table["id"], "name": new_table["name"]}

@router.delete("/api/tables/{table_id}")
async def delete_table(request: Request, table_id: str):
    """Elimina una mesa por su ID."""
    await require_auth(request)
    await db.db_delete_table(table_id)
    return {"success": True}

@router.get("/menu/{table_id}", response_class=HTMLResponse)
async def menu_page(table_id: str):
    p = STATIC / "html" / "menu.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="menu.html no encontrado en static/")
    return HTMLResponse(p.read_text(encoding="utf-8"))

@router.get("/api/public/menu-context/{table_id}")
async def public_menu_context(table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")

    wa_number = await get_table_wa_number(table)
    wa_msg = f"Hola! Estoy en {table['name']} [t:{table['id']}]"
    wa_url = f"https://wa.me/{wa_number}?text={urllib.parse.quote(wa_msg)}"
    
    menu = await db.db_get_menu(wa_number) or {}
    availability = await db.db_get_menu_availability()

    restaurant = await db.db_get_restaurant_by_bot_number(wa_number) or {}
    features = restaurant.get("features") or {}
    if isinstance(features, str):
        import json as _json
        try: features = _json.loads(features)
        except Exception: features = {}

    return {
        "table_name": table["name"],
        "wa_url": wa_url,
        "menu": menu,
        "availability": availability,
        "locale": features.get("locale", "en-US"),
        "currency": features.get("currency", "USD"),
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

class AdminCallRequest(BaseModel):
    phone: str = ""
    table_id: str = ""
    table_name: str = ""
    bot_number: str = ""

@router.post("/api/waiter-alerts/admin-call")
async def admin_call_waiter(request: Request, body: AdminCallRequest):
    """El administrador convoca a un mesero/empleado a caja o dashboard."""
    await require_auth(request)
    alert = await db.db_create_waiter_alert(
        phone=body.phone or "admin",
        bot_number=body.bot_number,
        alert_type="admin_call",
        message="El Administrador requiere verte en caja/dashboard",
        table_id=body.table_id,
        table_name=body.table_name,
    )
    return {"success": True, "alert": alert}

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
    username = await require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("DELETE FROM conversations WHERE phone = $1", phone)
            await conn.execute("DELETE FROM carts WHERE phone = $1", phone)
            await conn.execute("UPDATE table_sessions SET status = 'closed', closed_at = NOW(), closed_by = 'manual_delete', closed_by_username = $2 WHERE phone = $1 AND closed_at IS NULL", phone, username)
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
               AND status NOT IN ('en_camino', 'en_puerta', 'entregado', 'cancelado')
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
            "total": float(to_decimal(r["total"])),  # JSON boundary
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
        
        if new_status in ("en_camino", "entregado"):
            row = await conn.fetchrow("SELECT phone, address, total FROM orders WHERE id=$1", order_id)
            if row:
                phone = row["phone"]
                msg = f"🛵 ¡Tu pedido ya va en camino a {row['address']}! Pronto estaremos contigo." if new_status == "en_camino" else f"✅ ¡Tu pedido fue entregado! Total: ${int(row['total']):,} COP. ¡Gracias por tu compra!"
                try:
                    session = await conn.fetchrow("SELECT meta_phone_id FROM table_sessions WHERE phone=$1 ORDER BY started_at DESC LIMIT 1", phone)
                    db_phone_id = session["meta_phone_id"] if session else None
                except Exception:
                    db_phone_id = None
                await send_wa_msg(phone, msg, db_phone_id)
                
        if new_status == "confirmado":
            order_row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
            if order_row:
                restaurant = await get_current_restaurant(request)
                config = await billing.get_billing_config(restaurant["id"])
                
                features = restaurant.get("features") or {}
                if isinstance(features, str):
                    import json as _json
                    try:
                        features = _json.loads(features)
                    except Exception:
                        features = {}
                
                raw_dian = features.get("dian_active", False)
                if isinstance(raw_dian, str):
                    dian_active = raw_dian.strip().lower() in ("true", "1", "yes", "on")
                else:
                    dian_active = bool(raw_dian)

                items = order_row["items"]
                if isinstance(items, str):
                    import json as _json
                    items = _json.loads(items)

                if config and dian_active:
                    config["_restaurant_id"] = restaurant["id"]
                    provider = config.get("provider", "mesio_native")
                    adapter = billing.get_adapter(provider)
                    
                    order_for_billing = {
                        "id": order_id,
                        "total": float(to_decimal(order_row["total"])),      # JSON boundary
                        "subtotal": float(to_decimal(order_row["subtotal"])), # JSON boundary
                        "service_charge": 0.0,
                        "items": items,
                        "payment_method": order_row.get("payment_method", "cash"),
                        "order_ref": order_id,
                        "customer": {"name": "Consumidor Final", "nit": "222222222", "email": ""}
                    }
                    try:
                        await adapter.create_invoice(order_for_billing, config)
                    except Exception:
                        pass

    return {"success": True}

# ── TABLE ORDERS & OTHERS ──────────────────────────────────────────

@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None, station: str = None):
    """Devuelve órdenes de mesa filtradas por sucursal y estado."""
    user = await get_current_user(request)
    branch_id = user.get("branch_id")

    # 🛡️ FILTRO GLOBAL: Leer el selector del Topbar
    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and "owner" in user.get("role", ""):
        branch_id = int(branch_header)

    # Detectar si el usuario es admin/owner (puede ver todas las sucursales)
    role = user.get("role", "")
    is_admin = any(r in role for r in ("owner", "admin", "gerente"))

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if branch_id is not None:
            # Sucursal específica: filtro exacto
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status = $1 AND branch_id = $2 ORDER BY created_at ASC",
                    status, branch_id
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') AND branch_id = $1 ORDER BY created_at ASC",
                    branch_id
                )
        elif is_admin:
            # Owner/admin sin sucursal seleccionada: ve todas
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status = $1 ORDER BY created_at ASC",
                    status
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') ORDER BY created_at ASC"
                )
        else:
            # Staff en restaurante principal (branch_id IS NULL = restaurante matriz)
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status = $1 AND branch_id IS NULL ORDER BY created_at ASC",
                    status
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') AND branch_id IS NULL ORDER BY created_at ASC"
                )

    import json as _json
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('items'), str):
            try: d['items'] = _json.loads(d['items'])
            except: pass
        if d.get('created_at') and hasattr(d['created_at'], 'isoformat'):
            d['created_at'] = d['created_at'].isoformat() + 'Z'
        result.append(d)

    if station:
        result = [r for r in result if r.get("station", "all") in (station, "all")]

    return {"orders": result}

@router.get("/api/table-orders/{order_id}/ticket")
async def get_order_ticket(request: Request, order_id: str):
    """
    Devuelve los datos estructurados de un ticket/comanda agregando todas
    las sub-órdenes del mismo base_order_id.
    Incluye datos fiscales (CUFE, QR) si existe una factura emitida.
    """
    import json as _json
    user = await get_current_user(request)
    branch_id = user.get("branch_id")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if branch_id is not None:
            rows = await conn.fetch(
                """SELECT * FROM table_orders
                   WHERE (id = $1 OR base_order_id = $1) AND branch_id = $2
                   ORDER BY created_at ASC""",
                order_id, branch_id)
        else:
            rows = await conn.fetch(
                """SELECT * FROM table_orders
                   WHERE id = $1 OR base_order_id = $1
                   ORDER BY created_at ASC""",
                order_id)

        if not rows:
            raise HTTPException(status_code=404, detail="Orden no encontrada")

        # Agregar ítems y totales de todas las sub-órdenes
        all_items: list = []
        total: Decimal = Decimal("0")
        notes_parts: list = []
        first = dict(rows[0])

        for row in rows:
            d = dict(row)
            items = d.get("items", [])
            if isinstance(items, str):
                try:
                    items = _json.loads(items)
                except Exception:
                    items = []
            if isinstance(items, list):
                all_items.extend(items)
            total += to_decimal(d.get("total") or 0)
            if d.get("notes"):
                notes_parts.append(d["notes"])

        # Datos fiscales: última factura emitida para esta orden
        fiscal = None
        try:
            fiscal_row = await conn.fetchrow(
                """SELECT cufe, qr_data, invoice_number, issue_date,
                          tax_regime, tax_pct, dian_status, uuid_dian
                   FROM fiscal_invoices
                   WHERE order_id = $1
                   ORDER BY created_at DESC LIMIT 1""",
                order_id)
            if fiscal_row:
                fiscal = dict(fiscal_row)
        except Exception:
            pass  # tabla puede no existir en entornos sin billing

    created = first.get("created_at")
    if created and hasattr(created, "isoformat"):
        created = created.isoformat() + "Z"

    return {
        "order_id":   order_id,
        "table_name": first.get("table_name", ""),
        "created_at": created,
        "items":      all_items,
        "total":      float(total),  # JSON boundary: Decimal → float for display
        "notes":      " | ".join(notes_parts) if notes_parts else "",
        "fiscal":     fiscal,
    }


async def send_wa_msg(phone: str, text: str, db_phone_id: str = None):
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    final_phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_ID", "")

    if token and final_phone_id:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    f"https://graph.facebook.com/{META_API_VERSION}/{final_phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}}
                )
                print(f"✅ WA Notificación a {phone}: {resp.status_code}")
        except Exception as e:
            print(f"❌ Error enviando WhatsApp: {e}")
    else:
        print(f"⚠️ No se envió WA a {phone} -> Faltan variables (Token: {bool(token)}, PhoneID: {final_phone_id})")


async def send_wa_interactive_nps(phone: str, nps_label: str, db_phone_id: str = None):
    """Send the NPS rating question as an interactive WhatsApp message with a skip button."""
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    final_phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_ID", "")

    if not token or not final_phone_id:
        print(f"⚠️ No se envió NPS interactivo a {phone} -> Faltan variables")
        return

    nps_text = (
        f"⭐ Antes de irte, ¿cómo calificarías tu experiencia en {nps_label} hoy?\n"
        f"Responde con un número del 1 al 5\n"
        f"(1 = Muy mala · 5 = Excelente)"
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
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
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"https://graph.facebook.com/{META_API_VERSION}/{final_phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=payload
            )
            print(f"✅ WA NPS interactivo a {phone}: {resp.status_code}")
    except Exception as e:
        print(f"❌ Error enviando NPS interactivo: {e}")

@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    username = await require_auth(request)
    body = await request.json()
    status = body.get("status")
    
    valid_statuses = ['recibido', 'en_preparacion', 'listo', 'entregado', 'generar_factura', 'cerrar_mesa', 'factura_entregada', 'cancelado']
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Estado inválido")
    
    pool = await db.get_pool()
    
    async with pool.acquire() as conn:
        # 🛡️ FIX: Agregamos table_id a la consulta
        order_record = await conn.fetchrow("SELECT phone, table_name, base_order_id, table_id FROM table_orders WHERE id=$1", order_id)
        
    if not order_record:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    order = dict(order_record)
    phone = order.get("phone")
    table_name = order.get("table_name", "tu mesa")
    
    db_phone_id = None
    session_data = None
    if phone and phone != "manual":
        async with pool.acquire() as conn:
            try:
                session = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL", phone)
                if session:
                    session_data = dict(session)
                    db_phone_id = session_data.get("meta_phone_id")
            except Exception: pass

    if status == "generar_factura":
        base_id = order.get("base_order_id") or order_id
        await db.db_mark_factura_generada(base_id)
        if phone and phone != "manual":
            await send_wa_msg(
                phone,
                f"🧾 Estamos preparando tu factura de {table_name}. En un momento te la llevamos.",
                db_phone_id
            )
        return {"success": True, "order_id": order_id, "status": "factura_generada"}

    if status in ("cerrar_mesa", "factura_entregada"):
        base_id = order.get("base_order_id") or order_id
        await db.db_close_table_bill(base_id)
        if phone and phone != "manual":
            await _farewell_and_nps(phone, order.get("table_id"), session_data, db_phone_id, username)
        return {"success": True, "order_id": order_id, "status": "factura_entregada"}
        
    # ── C. ESTADOS NORMALES (Prep, Listo, Entregado) ──
    else:
        await db.db_update_table_order_status(order_id, status)
        if status == "entregado" and phone and _can_send_entregado_notif(phone):
            msg = f"¡Tu pedido ha llegado a {table_name}! 🍽️\n\n¡Que lo disfrutes! Cuando estés listo, puedes pedir la cuenta aquí mismo."
            await send_wa_msg(phone, msg, db_phone_id)

    return {"success": True, "order_id": order_id, "status": status}

@router.get("/cocina", response_class=HTMLResponse)
async def kitchen_display():
    return HTMLResponse((STATIC / "html" / "kitchen.html").read_text(encoding="utf-8"))

@router.get("/bar", response_class=HTMLResponse)
async def bar_display():
    p = STATIC / "html" / "bar.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="bar.html no encontrado en static/")
    return HTMLResponse(p.read_text(encoding="utf-8"))

# ── MÓDULO PUNTO DE VENTA (POS) PARA MESEROS ─────────────────────────

class ManualOrderRequest(BaseModel):
    table_id:   str
    table_name: str
    items:      list
    total:      int
    notes:      str = ""
    station:    str = "all"
    branch_id:  int = None  # 🛡️ Agregamos branch_id al modelo
    
@router.get("/api/pos/menu")
async def get_pos_menu(request: Request):
    """Devuelve el menú del restaurante para pintarlo en el POS del mesero"""
    user = await get_current_user(request)
    
    # Buscamos el número de WhatsApp asociado a la sucursal del mesero
    wa_number = ""
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r: wa_number = r.get("whatsapp_number", "")
    if not wa_number:
        all_r = await db.db_get_all_restaurants()
        if all_r: wa_number = all_r[0].get("whatsapp_number", "")
        
    menu = await db.db_get_menu(wa_number) or {}
    return {"menu": menu}

@router.get("/api/pos/tables-status")
async def get_tables_status(request: Request):
    """Devuelve todas las mesas y su estado actual (ideal para pintar el mapa)"""
    await require_auth(request)
    
    # 1. Resolución de contexto inteligente
    restaurant = await get_current_restaurant(request)
    is_main = restaurant.get("parent_restaurant_id") is None
    branch_id = None if is_main else restaurant["id"]
        
    tables = await db.db_get_tables(branch_id=branch_id, is_main=is_main)

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        active_sessions = await conn.fetch("SELECT table_id FROM table_sessions WHERE status IN ('active','nps_pending')")
        
        # 2. Separar las órdenes pendientes según arquitectura
        if not is_main:
            pending_orders = await conn.fetch(
                "SELECT table_id, status FROM table_orders WHERE status NOT IN ('factura_entregada', 'cancelado') AND branch_id = $1",
                branch_id
            )
        else:
            pending_orders = await conn.fetch(
                "SELECT table_id, status FROM table_orders WHERE status NOT IN ('factura_entregada', 'cancelado') AND branch_id IS NULL"
            )
        
    session_map = {s['table_id'] for s in active_sessions}
    order_map = {}
    for o in pending_orders:
        if o['table_id'] not in order_map:
            order_map[o['table_id']] = []
        order_map[o['table_id']].append(o['status'])
        
    for t in tables:
        tid = t['id']
        t['bot_active'] = tid in session_map
        t['pending_orders'] = order_map.get(tid, [])
        
    return {"tables": tables}

@router.patch("/api/table-orders/{base_order_id}/adjust")
async def adjust_table_bill(request: Request, base_order_id: str):
    """Ajusta ítems y total de una factura antes de cobrar (descuentos, propina, etc.)"""
    await require_auth(request)
    import json as _json

    body = await request.json()
    adjusted_items = body.get("items", [])
    new_total = to_decimal(body.get("total", 0))

    if new_total < 0:
        raise HTTPException(status_code=400, detail="El total no puede ser negativo")

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        base_row = await conn.fetchrow(
            "SELECT id FROM table_orders WHERE id=$1", base_order_id
        )
        if not base_row:
            raise HTTPException(status_code=404, detail="Orden no encontrada")

        # Update base order with the adjusted items and new total
        await conn.execute(
            "UPDATE table_orders SET items=$2, total=$3, updated_at=NOW() WHERE id=$1",
            base_order_id, _json.dumps(adjusted_items), new_total
        )
        # Zero out sub-orders so they don't double-count in the bill sum
        # Exclude the base order itself (base_order_id = id for the first order)
        await conn.execute(
            "UPDATE table_orders SET items='[]'::jsonb, total=0, updated_at=NOW() WHERE base_order_id=$1 AND id != $1",
            base_order_id
        )

    print(f"✏️ Factura ajustada: {base_order_id} → total={new_total}", flush=True)
    return {"success": True, "base_order_id": base_order_id, "new_total": float(new_total)}


@router.post("/api/pos/order")
async def pos_manual_order(request: Request, body: ManualOrderRequest):
    await require_auth(request)
    user = await get_current_user(request)
    
    # 🛡️ RESOLUCIÓN DE SUCURSAL
    # Si viene en el body lo usamos, si no, usamos el del usuario (mesero/admin)
    branch_id = body.branch_id or user.get("branch_id")
    
    order_id = f"pos-{str(uuid.uuid4())[:8]}"
    phone = "manual"
    base_id = await db.db_get_base_order_id(body.table_id)
    
    if base_id:
        final_base_id = base_id
        sub_num = await db.db_get_next_sub_number(base_id)
    else:
        final_base_id = order_id
        sub_num = 1

    order = {
        "id":            order_id,
        "table_id":      body.table_id,
        "table_name":    body.table_name,
        "phone":         phone,
        "items":         body.items,
        "status":        "recibido",
        "notes":         body.notes,
        "total":         body.total,
        "base_order_id": final_base_id,
        "sub_number":    sub_num,
        "station":       body.station,
        "branch_id":     branch_id
    }

    await db.db_save_table_order(order)

    dest = {"kitchen": "cocina", "bar": "bar", "all": "cocina y bar"}.get(body.station, "cocina")
    return {"success": True, "order_id": order_id, "message": f"Comanda enviada a {dest}"}


# ── SPLIT CHECKS / PAGOS MIXTOS (FASE 5) ──────────────────────────────────────

class CheckItem(BaseModel):
    name: str
    qty: int
    unit_price: float

class CheckDef(BaseModel):
    check_number: int
    items: list[CheckItem]

class CreateChecksBody(BaseModel):
    checks: list[CheckDef]
    tax_pct: float = 19.0        # enviado por el cliente desde la config de billing
    tax_regime: str = "iva"

class PaymentMethod(BaseModel):
    method: str    # efectivo | tarjeta | nequi | transferencia
    amount: float

class PayCheckBody(BaseModel):
    payments: list[PaymentMethod] = []
    customer_name: str = "Consumidor Final"
    customer_nit:  str = "222222222"
    customer_email: str = ""
    service_charge: float = 0.0  # Cargo de servicio en valor absoluto (ej. 10% del subtotal)
    tip_amount: float = Field(0.0, ge=0.0)


@router.post("/api/table-orders/{base_order_id}/checks")
async def create_checks(request: Request, base_order_id: str, body: CreateChecksBody):
    """
    Crea o reemplaza la división de cuenta de una mesa.
    Valida integridad de cantidades contra el ticket original.
    Calcula subtotal/impuesto/total servidor-side (no confía en el cliente).
    """
    user = await get_current_user(request)

    # Obtener el ticket completo para validar cantidades
    ticket = await db.db_get_order_ticket_data(base_order_id, user.get("branch_id") or None)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")

    # Mapa de qty disponible por plato en el ticket original
    available: dict[str, int] = {}
    for item in ticket.get("items", []):
        key = item["name"].strip().lower()
        available[key] = available.get(key, 0) + int(item.get("quantity", item.get("qty", 1)))

    # Validar que los checks no excedan las cantidades disponibles
    check_totals: dict[str, int] = {}
    for chk in body.checks:
        for it in chk.items:
            key = it.name.strip().lower()
            check_totals[key] = check_totals.get(key, 0) + it.qty
    for name, qty in check_totals.items():
        avail = available.get(name, 0)
        if qty > avail:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}': cantidad en checks ({qty}) supera la pedida ({avail})"
            )

    # Construir checks con totales calculados servidor-side
    tax_factor = to_decimal(body.tax_pct) / Decimal("100")
    validated = []
    for chk in body.checks:
        # Reconstruir items con unit_price desde el ticket (busca por nombre)
        price_map: dict[str, Decimal] = {}
        for item in ticket.get("items", []):
            price_map[item["name"].strip().lower()] = to_decimal(item.get("price", 0))

        items_out = []
        gross = Decimal("0")
        for it in chk.items:
            unit_price = price_map.get(it.name.strip().lower(), to_decimal(it.unit_price))
            items_out.append({
                "name": it.name, "qty": it.qty,
                "unit_price": float(unit_price),  # JSON boundary
                "subtotal": float(money_mul(unit_price, it.qty))  # JSON boundary
            })
            gross += money_mul(unit_price, it.qty)

        subtotal   = quantize_money(gross / (Decimal("1") + tax_factor))
        tax_amount = quantize_money(gross - subtotal)
        total      = quantize_money(gross)

        validated.append({
            "check_number": chk.check_number,
            "items": items_out,
            "subtotal": float(subtotal),   # JSON boundary: stored as NUMERIC via db
            "tax_amount": float(tax_amount),
            "total": float(total),
        })

    result = await db.db_create_checks(base_order_id, validated)
    return {"success": True, "checks": result}


@router.get("/api/table-orders/{base_order_id}/checks")
async def get_checks(request: Request, base_order_id: str):
    """Lista todos los checks de una mesa con sus datos fiscales."""
    await get_current_user(request)
    checks = await db.db_get_checks(base_order_id)
    return {"checks": checks}

@router.post("/api/table-orders/{base_order_id}/checks/{check_id}/pay")
async def pay_check(request: Request, base_order_id: str, check_id: str, body: PayCheckBody):
    try:
        restaurant = await get_current_restaurant(request)
        check = await db.db_get_check(check_id)
        
        if not check:
            raise HTTPException(status_code=404, detail="Check no encontrado")
        if check["base_order_id"] != base_order_id:
            raise HTTPException(status_code=400, detail="El check no pertenece a este ticket")
        if check["status"] != "open":
            raise HTTPException(status_code=400, detail=f"Este check ya fue procesado (status: {check['status']})")

        # Si no se enviaron pagos, usar proposed_payments del check (flujo bot)
        if not body.payments:
            proposed = check.get("proposed_payments")
            if isinstance(proposed, str):
                import json as _json
                proposed = _json.loads(proposed)
            if proposed:
                body.payments = [PaymentMethod(method=p["method"], amount=p["amount"]) for p in proposed]
            else:
                raise HTTPException(status_code=400, detail="No se especificaron métodos de pago")

        # También usar tip propuesto si no se envió tip explícito y hay uno guardado
        if body.tip_amount == 0.0 and check.get("proposed_tip"):
            body.tip_amount = float(to_decimal(check["proposed_tip"]))

        total_pagado = to_decimal(sum(p.amount for p in body.payments))
        check_total  = to_decimal(check["total"]) + to_decimal(body.service_charge)
        if total_pagado < check_total:
            raise HTTPException(status_code=400, detail=f"Pago insuficiente: se requieren ${float(check_total):,.0f}, se recibieron ${float(total_pagado):,.0f}")

        # Resolve currency before quantizing change/tip so zero-decimal currencies (COP, CLP)
        # are rounded correctly at this JSON boundary.
        features = restaurant.get("features") or {}
        if isinstance(features, str):
            import json as _json
            try:
                features = _json.loads(features)
            except Exception:
                features = {}
        _currency = features.get("currency") if isinstance(features, dict) else None

        change = float(quantize_money(total_pagado - check_total, _currency))

        tip_amount_d = to_decimal(body.tip_amount)
        if tip_amount_d > 0 and tip_amount_d > money_mul(check["total"], Decimal("0.5")):
            raise HTTPException(status_code=400, detail="La propina no puede superar el 50% del total")

        config = await billing.get_billing_config(restaurant["id"])

        raw_dian = features.get("dian_active", False)
        if isinstance(raw_dian, str):
            dian_active = raw_dian.strip().lower() in ("true", "1", "yes", "on")
        else:
            dian_active = bool(raw_dian)

        items = check.get("items", [])
        if isinstance(items, str):
            import json as _json
            items = _json.loads(items)

        _check_total_d = to_decimal(check["total"])
        _svc_charge_d  = to_decimal(body.service_charge)
        order_for_billing = {
            "id":             check_id,
            "total":          float(_check_total_d + _svc_charge_d),  # JSON boundary
            "subtotal":       float(_check_total_d),                   # JSON boundary
            "service_charge": float(_svc_charge_d),
            "items":          items,
            "payment_method": body.payments[0].method if body.payments else "cash",
            "order_ref":      base_order_id,
            "customer": {
                "name":  body.customer_name,
                "nit":   body.customer_nit,
                "email": body.customer_email,
            },
        }

        fiscal_invoice_id = None
        if config and dian_active:
            config["_restaurant_id"] = restaurant["id"]
            provider = config.get("provider", "mesio_native")
            adapter  = billing.get_adapter(provider)
            try:
                fiscal = await adapter.create_invoice(order_for_billing, config)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Error al emitir factura: {exc}")
            fiscal_invoice_id = fiscal["id"]
        else:
            fiscal = {"id": None, "local": True}

        payments_list = [{"method": p.method, "amount": p.amount} for p in body.payments]

        await db.db_finalize_check_payment(
            check_id=check_id,
            base_order_id=base_order_id,
            payments=payments_list,
            change_amount=change,
            fiscal_invoice_id=fiscal_invoice_id,
            customer_name=body.customer_name,
            customer_nit=body.customer_nit,
            customer_email=body.customer_email,
            tip_amount=body.tip_amount,
        )

        if hasattr(loyalty_svc, "accrue_on_check"):
            asyncio.create_task(loyalty_svc.accrue_on_check(
                restaurant_id=restaurant["id"],
                bot_number=restaurant.get("whatsapp_number", ""),
                base_order_id=base_order_id,
                check_id=check_id,
                total_cop=float(to_decimal(check["total"]) + to_decimal(body.service_charge)),
            ))
        else:
            print(f"⚠️ Aviso: 'accrue_on_check' no está implementado en loyalty.py. Saltando puntos para el check {check_id}.")

        order_row = await db.db_get_first_table_order(base_order_id)
        if order_row and order_row["status"] == "factura_entregada":
            customer_phone = order_row.get("phone")
            if customer_phone and customer_phone != "manual":
                sess = await db.db_get_open_session_by_phone(customer_phone)
                session_phone_id = sess.get("meta_phone_id") if sess else None
                await _farewell_and_nps(customer_phone, order_row.get("table_id"), sess, session_phone_id, "caja")

        return {
            "success":  True,
            "check_id": check_id,
            "change":   change,
            "fiscal":   fiscal,
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {str(e)}")


class CheckoutProofBody(BaseModel):
    media_url: str
    customer_phone: str

@router.post("/api/table-orders/{base_order_id}/checkout-proposal/proof")
async def attach_checkout_proof(
    request: Request,
    base_order_id: str,
    body: CheckoutProofBody,
):
    """Adjunta comprobante de pago a los checks con propuesta awaiting_proof."""
    await get_current_user(request)
    updated = await db.db_attach_proof(base_order_id, body.customer_phone, body.media_url)
    if not updated:
        raise HTTPException(status_code=404, detail="No hay propuesta awaiting_proof para este teléfono")
    return {"success": True}


@router.get("/api/checkout-proposals")
async def list_checkout_proposals(request: Request):
    """
    Lista mesas con propuestas de pago bot activas (pending/awaiting_proof/proof_received).
    Para el tab 'Por Confirmar' en caja.html.
    """
    restaurant = await get_current_restaurant(request)
    branch_header = request.headers.get("X-Branch-ID", "")

    branch_ids = None
    if branch_header and branch_header != "all":
        try:
            branch_ids = [int(branch_header)]
        except ValueError:
            pass

    proposals = await db.db_list_checkout_proposals(restaurant["id"], branch_ids)
    return {"proposals": proposals}


@router.get("/api/table-orders/{base_order_id}/checks/{check_id}/ticket")
async def get_check_ticket(request: Request, base_order_id: str, check_id: str):
    """Devuelve los datos del check para impresión de factura térmica."""
    await get_current_user(request)
    ticket = await db.db_get_check_ticket(check_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Check no encontrado")
    return ticket


@router.delete("/api/table-orders/{base_order_id}/checks/{check_id}")
async def delete_check(request: Request, base_order_id: str, check_id: str):
    """Elimina un check en estado 'open'. No afecta checks ya cobrados."""
    await get_current_user(request)
    deleted = await db.db_delete_open_check(check_id)
    if not deleted:
        raise HTTPException(
            status_code=400,
            detail="No se puede eliminar: el check no existe o ya fue procesado"
        )
    return {"success": True}