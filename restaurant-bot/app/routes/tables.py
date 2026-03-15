import uuid
import urllib.parse
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.services import database as db
from app.services.auth import verify_token
from app.data.restaurant import RESTAURANT_INFO, MENU

router = APIRouter()

# Número de WhatsApp del restaurante (usar el real cuando esté configurado)
WA_NUMBER = "14155238886"  # Twilio sandbox por defecto


def require_auth(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="No autorizado")


# ── GESTIÓN DE MESAS ──────────────────────────────────────

class TableRequest(BaseModel):
    number: int
    name: str = ""


@router.get("/api/tables")
async def get_tables(request: Request):
    require_auth(request)
    tables = await db.db_get_tables()
    return {"tables": tables}


@router.post("/api/tables")
async def create_table(request: Request, body: TableRequest):
    require_auth(request)
    table_id = f"mesa-{body.number}"
    name = body.name or f"Mesa {body.number}"
    await db.db_create_table(table_id, body.number, name)
    return {"success": True, "table_id": table_id, "name": name}


@router.delete("/api/tables/{table_id}")
async def delete_table(request: Request, table_id: str):
    require_auth(request)
    await db.db_delete_table(table_id)
    return {"success": True}


# ── QR GENERATION ────────────────────────────────────────

@router.get("/api/tables/{table_id}/qr", response_class=HTMLResponse)
async def get_table_qr(table_id: str):
    """Devuelve SVG del QR generado en el navegador con qrcode.js."""
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")
    table_name = table['name']
    wa_text = f"Hola! Estoy en {table_name} y quiero hacer un pedido"
    wa_url = f"https://wa.me/{WA_NUMBER}?text={urllib.parse.quote(wa_text)}"
    # Return minimal HTML that renders QR as canvas and redirects to PNG
    return f'''<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
</head><body style="margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;">
<div id="qr"></div>
<script>
new QRCode(document.getElementById("qr"), {{
  text: "{wa_url}",
  width: 300, height: 300,
  colorDark: "#0D1412", colorLight: "#ffffff",
  correctLevel: QRCode.CorrectLevel.M
}});
</script>
</body></html>'''


@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(table_id: str):
    """Página HTML lista para imprimir con el QR de la mesa."""
    table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")

    table_name = table['name']
    wa_text = f"Hola! Estoy en {table_name} y quiero hacer un pedido"
    wa_url = f"https://wa.me/{WA_NUMBER}?text={wa_text.replace(' ', '%20')}"

    qr_url = f"https://chart.googleapis.com/chart?chs=300x300&cht=qr&chl={urllib.parse.quote(wa_url)}&choe=UTF-8"
    qr_b64 = None

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Arial', sans-serif; background: #fff; }}
  .page {{ width: 10cm; margin: 1cm auto; text-align: center; padding: 1.5cm; border: 2px solid #0D1412; border-radius: 16px; }}
  .logo {{ font-size: 28px; font-weight: 900; color: #0D1412; margin-bottom: 4px; }}
  .logo span {{ color: #1D9E75; }}
  .table-name {{ font-size: 20px; font-weight: 700; color: #0D1412; margin: 12px 0 4px; }}
  .instruction {{ font-size: 13px; color: #666; margin-bottom: 16px; line-height: 1.5; }}
  .qr-img {{ width: 200px; height: 200px; margin: 0 auto 16px; display: block; border-radius: 8px; }}
  .wa-badge {{ display: inline-flex; align-items: center; gap: 6px; background: #25D366; color: white; padding: 8px 16px; border-radius: 100px; font-size: 13px; font-weight: 600; margin-bottom: 16px; }}
  .steps {{ text-align: left; background: #f8f8f5; border-radius: 10px; padding: 12px 16px; margin-top: 8px; }}
  .step {{ font-size: 12px; color: #444; padding: 3px 0; display: flex; gap: 8px; }}
  .step-num {{ color: #1D9E75; font-weight: 700; }}
  @media print {{ body {{ margin: 0; }} }}
  #qr-container canvas, #qr-container img {{ width: 200px !important; height: 200px !important; border-radius: 8px; }}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
</head>
<body>
<div class="page">
  <div class="logo">Mesio<span>.</span></div>
  <div class="table-name">{table_name}</div>
  <div class="instruction">Escanea el código QR con tu celular<br>y pide directamente por WhatsApp</div>
  <div id="qr-container" style="width:200px;height:200px;margin:0 auto 16px;"></div>
  <div class="wa-badge">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="white"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51a9 9 0 00-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z"/></svg>
    Pedir por WhatsApp
  </div>
  <div class="steps">
    <div class="step"><span class="step-num">1.</span><span>Abre la cámara de tu celular</span></div>
    <div class="step"><span class="step-num">2.</span><span>Apunta al código QR</span></div>
    <div class="step"><span class="step-num">3.</span><span>Se abre WhatsApp automáticamente</span></div>
    <div class="step"><span class="step-num">4.</span><span>Envía el mensaje y haz tu pedido</span></div>
  </div>
</div>
<script>
window.onload = function() {
  new QRCode(document.getElementById('qr-container'), {
    text: '{wa_url}',
    width: 200, height: 200,
    colorDark: '#0D1412', colorLight: '#ffffff',
    correctLevel: QRCode.CorrectLevel.M
  });
  setTimeout(function() {{ window.print(); }}, 800);
};
</script>
</body>
</html>"""
    return HTMLResponse(html)


# ── PEDIDOS DE MESA ──────────────────────────────────────

@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None):
    require_auth(request)
    orders = await db.db_get_table_orders(status)
    return {"orders": orders}


@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    require_auth(request)
    body = await request.json()
    status = body.get("status")
    valid = ['recibido', 'en_preparacion', 'listo', 'entregado', 'cancelado']
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Estado debe ser uno de: {valid}")
    await db.db_update_table_order_status(order_id, status)
    return {"success": True, "order_id": order_id, "status": status}


# ── KITCHEN DISPLAY (sin auth — para tablet en cocina) ───

@router.get("/cocina", response_class=HTMLResponse)
async def kitchen_display():
    from pathlib import Path
    static = Path(__file__).parent.parent / "static"
    return (static / "kitchen.html").read_text()
