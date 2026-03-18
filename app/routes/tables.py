import os
import httpx
import urllib.parse
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.services import database as db
from app.services.auth import verify_token

router = APIRouter()


async def require_auth(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not await verify_token(token):
        raise HTTPException(status_code=401, detail="No autorizado")


async def _get_bot_number(request: Request) -> str:
    token      = request.headers.get("Authorization", "").replace("Bearer ", "")
    username   = await verify_token(token)
    user       = await db.db_get_user(username) if username else None
    bot_number = ""
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r:
            bot_number = r.get("whatsapp_number", "")
    if not bot_number:
        all_r = await db.db_get_all_restaurants()
        if all_r:
            bot_number = all_r[0].get("whatsapp_number", "")
    return bot_number


async def _send_whatsapp_text(phone: str, message: str, phone_id_override: str = "") -> bool:
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = phone_id_override or os.getenv("META_PHONE_NUMBER_ID", "")
    if not token or not phone_id:
        print(f"⚠️ No se puede enviar WA: token={'ok' if token else 'FALTA'} phone_id={'ok' if phone_id else 'FALTA'}", flush=True)
        return False
    clean_phone = phone.lstrip("+").replace(" ", "")
    url  = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    body = {"messaging_product": "whatsapp", "to": clean_phone, "type": "text", "text": {"body": message}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            if resp.status_code == 200:
                return True
            print(f"⚠️ Meta API error {resp.status_code}: {resp.text}", flush=True)
            return False
    except Exception as e:
        print(f"⚠️ Error enviando WhatsApp: {e}", flush=True)
        return False


# ── MESAS ────────────────────────────────────────────────────────────

class TableRequest(BaseModel):
    number: int
    name: str = ""
    branch_id: int = None


@router.get("/api/tables")
async def get_tables(request: Request):
    await require_auth(request)
    token    = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user     = await db.db_get_user(username) if username else None
    return {"tables": await db.db_get_tables(branch_id=user.get("branch_id") if user else None)}


@router.post("/api/tables")
async def create_table(request: Request, body: TableRequest):
    await require_auth(request)
    token    = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token)
    user     = await db.db_get_user(username) if username else None
    branch_id = body.branch_id or (user.get("branch_id") if user else None)
    table_id  = f"{f'b{branch_id}-' if branch_id else ''}mesa-{body.number}"
    name      = body.name or f"Mesa {body.number}"
    await db.db_create_table(table_id, body.number, name, branch_id=branch_id)
    return {"success": True, "table_id": table_id, "name": name}


@router.delete("/api/tables/{table_id}")
async def delete_table(request: Request, table_id: str):
    await require_auth(request)
    await db.db_delete_table(table_id)
    return {"success": True}


# ── QR ───────────────────────────────────────────────────────────────

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


@router.get("/api/tables/{table_id}/qr", response_class=HTMLResponse)
async def get_table_qr(request: Request, table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    wa_number  = await get_table_wa_number(table)
    branch_key = f" [branch={table.get('branch_id')}]" if table.get("branch_id") else ""
    wa_url     = "https://wa.me/" + wa_number + "?text=" + urllib.parse.quote("Hola! Estoy en " + table["name"] + branch_key + " y quiero hacer un pedido")
    encoded    = urllib.parse.quote(wa_url)
    return HTMLResponse(
        f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
        f"</head><body style='margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;'>"
        f"<div id='qr'></div>"
        f"<script>window.onload=function(){{new QRCode(document.getElementById('qr'),{{text:decodeURIComponent('{encoded}'),width:300,height:300,colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});}}</script>"
        f"</body></html>"
    )


@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(request: Request, table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    wa_number  = await get_table_wa_number(table)
    branch_key = f" [branch={table.get('branch_id')}]" if table.get("branch_id") else ""
    encoded    = urllib.parse.quote("https://wa.me/" + wa_number + "?text=" + urllib.parse.quote("Hola! Estoy en " + table["name"] + branch_key + " y quiero hacer un pedido"))
    return HTMLResponse(
        f"<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'>"
        f"<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{font-family:Arial,sans-serif;background:#fff;}}"
        f".page{{width:10cm;margin:1cm auto;text-align:center;padding:1.5cm;border:2px solid #0D1412;border-radius:16px;}}"
        f".logo{{font-size:28px;font-weight:900;color:#0D1412;margin-bottom:4px;}}.logo span{{color:#1D9E75;}}"
        f".tname{{font-size:20px;font-weight:700;color:#0D1412;margin:12px 0 4px;}}"
        f".instr{{font-size:13px;color:#666;margin-bottom:16px;line-height:1.5;}}"
        f".qrbox{{width:200px;height:200px;margin:0 auto 16px;}}"
        f".qrbox canvas,.qrbox img{{width:200px !important;height:200px !important;border-radius:8px;}}"
        f".wa-badge{{display:inline-flex;align-items:center;gap:6px;background:#25D366;color:white;padding:8px 16px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:16px;}}"
        f".steps{{text-align:left;background:#f8f8f5;border-radius:10px;padding:12px 16px;margin-top:8px;}}"
        f".step{{font-size:12px;color:#444;padding:3px 0;display:flex;gap:8px;}}.sn{{color:#1D9E75;font-weight:700;}}"
        f"@media print{{body{{margin:0;}}}}</style>"
        f"<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
        f"</head><body><div class='page'>"
        f"<div class='logo'>Mesio<span>.</span></div><div class='tname'>{table['name']}</div>"
        f"<div class='instr'>Escanea el QR con tu celular<br>y pide por WhatsApp</div>"
        f"<div class='qrbox' id='qrc'></div>"
        f"<div class='wa-badge'>Pedir por WhatsApp</div>"
        f"<div class='steps'>"
        f"<div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div>"
        f"<div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div>"
        f"<div class='step'><span class='sn'>3.</span><span>Se abre WhatsApp automáticamente</span></div>"
        f"<div class='step'><span class='sn'>4.</span><span>Envía el mensaje y haz tu pedido</span></div>"
        f"</div></div>"
        f"<script>window.onload=function(){{new QRCode(document.getElementById('qrc'),{{text:decodeURIComponent('{encoded}'),width:200,height:200,colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}})}};"
        f"setTimeout(function(){{window.print();}},800);}}</script></body></html>"
    )


# ── TABLE ORDERS ─────────────────────────────────────────────────────

@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None):
    await require_auth(request)
    return {"orders": await db.db_get_table_orders(status)}


@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    await require_auth(request)
    body       = await request.json()
    new_status = body.get("status")
    if new_status not in ["recibido", "en_preparacion", "listo", "entregado", "factura_entregada", "cancelado"]:
        raise HTTPException(status_code=400, detail="Estado inválido")

    await db.db_update_table_order_status(order_id, new_status)

    # Cuando se entrega la comida → actualizar sesión + mensaje empático al cliente
    if new_status == "entregado":
        try:
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    "SELECT phone, total FROM table_orders WHERE id=$1", order_id
                )
            if order_row:
                phone = order_row["phone"]
                total = order_row["total"] or 0
                async with pool.acquire() as conn2:
                    session_row = await conn2.fetchrow(
                        """SELECT bot_number, meta_phone_id FROM table_sessions
                           WHERE phone=$1 AND status='active'
                           ORDER BY started_at DESC LIMIT 1""",
                        phone
                    )
                if session_row and session_row["bot_number"]:
                    bot_number   = session_row["bot_number"]
                    meta_phone_id = session_row["meta_phone_id"] or ""
                    await db.db_session_mark_delivered(phone, bot_number, total)
                    print(f"✅ Pedido {order_id} marcado como entregado → sesión actualizada para {phone}", flush=True)
                    sent = await _send_whatsapp_text(
                        phone,
                        "¡Que disfruten mucho su comida! 😊🍽️ Estoy aquí por si necesitan algo adicional — más bebidas, otro plato o lo que se les antoje. ¡Buen provecho!",
                        phone_id_override=meta_phone_id
                    )
                    print(f"📨 Mensaje empático enviado: {sent}", flush=True)
                else:
                    print(f"⚠️ No hay sesión activa para {phone}", flush=True)
        except Exception as e:
            import traceback
            print(f"⚠️ update_order_status entregado error: {traceback.format_exc()}", flush=True)

    # Cuando se entrega la factura → mensaje de despedida + cerrar sesión
    if new_status == "factura_entregada":
        try:
            pool = await db.get_pool()
            async with pool.acquire() as conn:
                order_row = await conn.fetchrow(
                    "SELECT phone FROM table_orders WHERE id=$1", order_id
                )
            if order_row:
                phone = order_row["phone"]
                async with pool.acquire() as conn2:
                    session_row = await conn2.fetchrow(
                        """SELECT bot_number, meta_phone_id FROM table_sessions
                           WHERE phone=$1 AND status='active'
                           ORDER BY started_at DESC LIMIT 1""",
                        phone
                    )
                if session_row and session_row["bot_number"]:
                    bot_number    = session_row["bot_number"]
                    meta_phone_id = session_row["meta_phone_id"] or ""
                    await _send_whatsapp_text(
                        phone,
                        "¡Fue un placer atenderles! 🙏✨ Esperamos verlos pronto. Si en algún momento desean pedir algo más, escaneen el código QR de la mesa y con gusto los atendemos. ¡Hasta pronto! 👋",
                        phone_id_override=meta_phone_id
                    )
                    await db.db_close_session(phone=phone, bot_number=bot_number, reason="factura_entregada", closed_by_username="mesero")
                    async with pool.acquire() as conn3:
                        await conn3.execute("DELETE FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
                    print(f"👋 Sesión cerrada post-factura para {phone}", flush=True)
        except Exception as e:
            import traceback
            print(f"⚠️ update_order_status factura_entregada error: {traceback.format_exc()}", flush=True)

    return {"success": True, "order_id": order_id, "status": new_status}


@router.post("/api/table-orders/{order_id}/clear-additional")
async def clear_order_additional(request: Request, order_id: str):
    """Limpia los items_additional de una orden sin cambiar su status.
    Se usa cuando cocina ya preparó el adicional de un pedido que estaba en 'listo',
    para quitarlo de la vista sin interrumpir el flujo del pedido principal."""
    await require_auth(request)
    await db.db_clear_additional(order_id)
    print(f"🧹 Adicional limpiado en orden {order_id}", flush=True)
    return {"success": True, "order_id": order_id}


@router.get("/cocina", response_class=HTMLResponse)
async def kitchen_display():
    from pathlib import Path
    return (Path(__file__).parent.parent / "static" / "kitchen.html").read_text()


# ── WAITER ALERTS ────────────────────────────────────────────────────

@router.get("/api/waiter-alerts")
async def get_waiter_alerts(request: Request):
    await require_auth(request)
    await db.db_init_waiter_alerts()
    bot_number = await _get_bot_number(request)
    return {"alerts": await db.db_get_waiter_alerts(bot_number)}


@router.post("/api/waiter-alerts/{alert_id}/dismiss")
async def dismiss_waiter_alert(alert_id: int, request: Request):
    await require_auth(request)
    await db.db_dismiss_waiter_alert(alert_id)
    return {"success": True}


# ── TABLE SESSIONS ───────────────────────────────────────────────────

@router.get("/api/table-sessions")
async def get_active_sessions(request: Request):
    await require_auth(request)
    await db.db_init_table_sessions()
    bot_number = await _get_bot_number(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM table_sessions WHERE bot_number=$1 AND status='active' ORDER BY started_at DESC",
            bot_number
        )
    return {"sessions": [db._serialize(dict(r)) for r in rows]}


@router.get("/api/table-sessions/closed")
async def get_closed_sessions(request: Request, hours: int = 24):
    await require_auth(request)
    await db.db_init_table_sessions()
    bot_number = await _get_bot_number(request)
    return {"sessions": await db.db_get_closed_sessions(bot_number, hours=hours)}


@router.post("/api/table-sessions/{session_id}/close")
async def close_table_session(session_id: int, request: Request):
    await require_auth(request)
    token    = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token) or ""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT phone, bot_number, table_name FROM table_sessions WHERE id=$1 AND status='active'",
            session_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Sesión no encontrada o ya cerrada")
        phone, bot_number, table_name = row["phone"], row["bot_number"], row["table_name"]
    await db.db_close_session(phone=phone, bot_number=bot_number, reason="waiter_manual", closed_by_username=username)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
    print(f"👋 Mesero '{username}' cerró sesión {session_id} — {table_name} ({phone})", flush=True)
    return {"success": True, "table_name": table_name, "phone": phone, "closed_by": username}


@router.post("/api/table-sessions/{session_id}/reopen")
async def reopen_table_session(session_id: int, request: Request):
    await require_auth(request)
    token    = request.headers.get("Authorization", "").replace("Bearer ", "")
    username = await verify_token(token) or ""
    session  = await db.db_reopen_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o no está cerrada")
    print(f"♻️ Admin '{username}' reabrió sesión {session_id} — {session['table_name']}", flush=True)
    return {"success": True, "session": session}


@router.get("/api/table-sessions/{session_id}/history")
async def get_session_history(session_id: int, request: Request):
    await require_auth(request)
    session = await db.db_get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    details = await db.db_get_conversation_details(session["phone"], session["bot_number"])
    return {
        "session": session,
        "history": details.get("history", []),
        "note": "Historial disponible solo si la sesión no fue limpiada."
    }


@router.post("/api/table-sessions/{session_id}/send-message")
async def send_message_to_client(session_id: int, request: Request):
    await require_auth(request)
    body    = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")
    session = await db.db_get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    full_message = f"🏠 *Restaurante:* {message}"
    ok = await _send_whatsapp_text(session["phone"], full_message)
    if not ok:
        raise HTTPException(status_code=502, detail="No se pudo enviar. Revisa META_ACCESS_TOKEN.")
    print(f"📨 Admin → {session['phone']} ({session.get('table_name','')}): {message}", flush=True)
    return {"success": True, "phone": session["phone"], "table_name": session.get("table_name", ""), "message": full_message}


@router.post("/api/table-sessions/{session_id}/alert-waiter")
async def alert_waiter_from_admin(session_id: int, request: Request):
    await require_auth(request)
    body           = await request.json()
    custom_message = (body.get("message") or "").strip()
    session        = await db.db_get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    alert_message = custom_message or f"El administrador solicita atención en {session.get('table_name','la mesa')}."
    await db.db_init_waiter_alerts()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO waiter_alerts (table_id, table_name, phone, bot_number, alert_type, message, dismissed)
            VALUES ($1, $2, $3, $4, 'admin_alert', $5, false) RETURNING id
        """, session.get("table_id",""), session.get("table_name",""),
            session.get("phone",""), session.get("bot_number",""), alert_message)
    print(f"🔔 Admin alertó meseros → {session.get('table_name','')} ({session.get('phone','')}): {alert_message}", flush=True)
    return {"success": True, "alert_id": row["id"] if row else None,
            "table_name": session.get("table_name",""), "message": alert_message}