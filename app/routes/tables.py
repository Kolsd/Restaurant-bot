import uuid
import urllib.parse
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.services import database as db
from app.services.auth import verify_token

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# AUTH HELPER
# ─────────────────────────────────────────────────────────────────────────────

async def require_auth(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not await verify_token(token):
        raise HTTPException(status_code=401, detail="No autorizado")


async def _get_bot_number(request: Request) -> str:
    """Devuelve el whatsapp_number del restaurante del usuario autenticado."""
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


# ─────────────────────────────────────────────────────────────────────────────
# MESAS
# ─────────────────────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# QR
# ─────────────────────────────────────────────────────────────────────────────

def build_qr_html(wa_number: str, table_name: str, table_id: str, width: int = 300, branch_id: int = None) -> str:
    branch_key = f" [branch={branch_id}]" if branch_id else ""
    wa_url = (
        "https://wa.me/" + wa_number + "?text="
        + urllib.parse.quote("Hola! Estoy en " + table_name + branch_key + " y quiero hacer un pedido")
    )
    return (
        f"<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
        f"</head><body style='margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;'>"
        f"<div id='qr'></div>"
        f"<script>window.onload=function(){{new QRCode(document.getElementById('qr'),{{"
        f"text:decodeURIComponent('{urllib.parse.quote(wa_url)}'),width:{width},height:{width},"
        f"colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});}};\"</script>"
        f"</body></html>"
    )


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
    wa_number = await get_table_wa_number(table)
    return build_qr_html(wa_number, table["name"], table_id, width=300, branch_id=table.get("branch_id"))


@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(request: Request, table_id: str):
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    wa_number  = await get_table_wa_number(table)
    branch_key = f" [branch={table.get('branch_id')}]" if table.get("branch_id") else ""
    encoded    = urllib.parse.quote(
        "https://wa.me/" + wa_number + "?text="
        + urllib.parse.quote("Hola! Estoy en " + table["name"] + branch_key + " y quiero hacer un pedido")
    )
    return HTMLResponse(
        f"<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'>"
        f"<style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{font-family:Arial,sans-serif;background:#fff;}}"
        f".page{{width:10cm;margin:1cm auto;text-align:center;padding:1.5cm;border:2px solid #0D1412;border-radius:16px;}}"
        f".logo{{font-size:28px;font-weight:900;color:#0D1412;margin-bottom:4px;}}.logo span{{color:#1D9E75;}}"
        f".tname{{font-size:20px;font-weight:700;color:#0D1412;margin:12px 0 4px;}}"
        f".instr{{font-size:13px;color:#666;margin-bottom:16px;line-height:1.5;}}"
        f".qrbox{{width:200px;height:200px;margin:0 auto 16px;}}"
        f".qrbox canvas,.qrbox img{{width:200px !important;height:200px !important;border-radius:8px;}}"
        f".wa-badge{{display:inline-flex;align-items:center;gap:6px;background:#25D366;color:white;"
        f"padding:8px 16px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:16px;}}"
        f".steps{{text-align:left;background:#f8f8f5;border-radius:10px;padding:12px 16px;margin-top:8px;}}"
        f".step{{font-size:12px;color:#444;padding:3px 0;display:flex;gap:8px;}}"
        f".sn{{color:#1D9E75;font-weight:700;}}@media print{{body{{margin:0;}}}}</style>"
        f"<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
        f"</head><body><div class='page'>"
        f"<div class='logo'>Mesio<span>.</span></div>"
        f"<div class='tname'>{table['name']}</div>"
        f"<div class='instr'>Escanea el QR con tu celular<br>y pide por WhatsApp</div>"
        f"<div class='qrbox' id='qrc'></div>"
        f"<div class='wa-badge'>Pedir por WhatsApp</div>"
        f"<div class='steps'>"
        f"<div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div>"
        f"<div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div>"
        f"<div class='step'><span class='sn'>3.</span><span>Se abre WhatsApp automáticamente</span></div>"
        f"<div class='step'><span class='sn'>4.</span><span>Envía el mensaje y haz tu pedido</span></div>"
        f"</div></div>"
        f"<script>window.onload=function(){{new QRCode(document.getElementById('qrc'),{{"
        f"text:decodeURIComponent('{encoded}'),width:200,height:200,"
        f"colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}})}}"
        f";setTimeout(function(){{window.print();}},800);}};</script></body></html>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TABLE ORDERS (cocina)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None):
    await require_auth(request)
    return {"orders": await db.db_get_table_orders(status)}


@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    await require_auth(request)
    body = await request.json()
    if body.get("status") not in ["recibido", "en_preparacion", "listo", "entregado", "cancelado"]:
        raise HTTPException(status_code=400, detail="Estado inválido")
    await db.db_update_table_order_status(order_id, body["status"])
    return {"success": True, "order_id": order_id, "status": body["status"]}


@router.get("/cocina", response_class=HTMLResponse)
async def kitchen_display():
    from pathlib import Path
    return (Path(__file__).parent.parent / "static" / "kitchen.html").read_text()


# ─────────────────────────────────────────────────────────────────────────────
# WAITER ALERTS
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/waiter-alerts")
async def get_waiter_alerts(request: Request):
    """
    Devuelve alertas activas (no descartadas, últimas 2h) para el restaurante
    del usuario autenticado.
    Polled cada 5s desde mesero.html.
    """
    await require_auth(request)
    await db.db_init_waiter_alerts()          # idempotente — crea la tabla si no existe
    bot_number = await _get_bot_number(request)
    alerts     = await db.db_get_waiter_alerts(bot_number)
    return {"alerts": alerts}


@router.post("/api/waiter-alerts/{alert_id}/dismiss")
async def dismiss_waiter_alert(alert_id: int, request: Request):
    """Marca una alerta como descartada (el mesero la atendió)."""
    await require_auth(request)
    await db.db_dismiss_waiter_alert(alert_id)
    return {"success": True}