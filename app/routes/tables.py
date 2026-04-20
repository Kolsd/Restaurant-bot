import asyncio
import html as _html
import os
import httpx
import urllib.parse
import uuid
from decimal import Decimal
from pathlib import Path
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from app.services import database as db
from app.services import billing
from app.services import state_store
from app.services.agent import trigger_nps
from app.routes.deps import require_auth, get_current_user, get_current_restaurant, get_current_restaurant_scoped
from app.services.tenant_context import tenant_scope, bypass_tenant_scope
from app.services import loyalty as loyalty_svc
from app.services.money import to_decimal, money_mul, quantize_money, money_sum
from app.services.logging import get_logger
from app.repositories import tables_repo as tr

log = get_logger(__name__)

router = APIRouter()
STATIC = Path(__file__).parent.parent / "static"
META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")
_APP_DOMAIN = os.getenv("APP_DOMAIN", "")

# Role-based status transition map: which roles may set each status
_STATUS_ROLE_MAP: dict[str, set[str]] = {
    'recibido':         {'cocina', 'caja', 'mesero', 'admin', 'owner', 'gerente'},
    'en_preparacion':   {'cocina', 'admin', 'owner', 'gerente'},
    'listo':            {'cocina', 'admin', 'owner', 'gerente'},
    'entregado':        {'mesero', 'caja', 'admin', 'owner', 'gerente'},
    'generar_factura':  {'caja', 'admin', 'owner', 'gerente'},
    'cerrar_mesa':      {'caja', 'admin', 'owner', 'gerente'},
    'factura_entregada':{'caja', 'admin', 'owner', 'gerente'},
    'cancelado':        {'caja', 'mesero', 'admin', 'owner', 'gerente'},
}

# WA notification rate-limiting moved to Redis via state_store (multi-worker safe).
# Keys: notif_wa:{bot_number}:{phone}:{kind}  max 1 per 5 min per worker pool.

async def get_table_wa_number(table: dict) -> str:
    """Resolve the WhatsApp number to use for a wa.me link from a table dict.

    Wave-2: tables belong to a SPECIFIC sede (branch_id = location_id), and
    each sede has its own whatsapp_number on the org+location join. We must
    NOT fall back to "any restaurant globally" — that's cross-tenant data
    leakage (the link would point to another customer's WhatsApp).

    If branch_id is missing or no restaurant resolves, return empty string —
    the caller renders the page without a wa.me link rather than with a
    wrong/cross-tenant one.
    """
    wa_number = ""
    bid = table.get("branch_id")
    if bid:
        r = await db.db_get_restaurant_by_id(bid)
        if r:
            wa_number = r.get("whatsapp_number", "") or ""

    # 🛡️ Limpiamos el sufijo _b para que el enlace wa.me sea válido
    return wa_number.split("_b")[0] if wa_number else ""

async def _get_restaurant_for_table(table_id: str | None, session_data: dict | None) -> dict:
    """Resuelve el restaurante/sucursal a partir de la mesa o la sesión activa."""
    if table_id:
        with bypass_tenant_scope("_get_restaurant_for_table: table lookup by ID"):
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
    # Wave-2: NO cross-tenant fallback. Returning "any restaurant globally"
    # used to mask resolution failures by happening to point at SOME tenant —
    # in single-tenant dev that worked; in production it would return another
    # customer's restaurant dict for a phone we cannot identify. Fail open
    # with an empty dict; callers (e.g. _farewell_and_nps) already short-circuit
    # on missing whatsapp_number so this degrades gracefully without leaking.
    return {}

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
        with bypass_tenant_scope("farewell_and_nps: mark session nps_pending by phone"):
            await db.db_mark_session_nps_pending(phone, final_bot_num)

    with bypass_tenant_scope("farewell_and_nps: cleanup checkout data by phone"):
        await db.db_cleanup_after_checkout(phone)

# ── MESAS ────────────────────────────────────────────────────────────

@router.get("/api/tables")
async def get_tables(request: Request):
    """Devuelve las mesas de la sucursal actual para pintarlas en el dashboard."""
    await require_auth(request)
    user = await get_current_user(request)

    # Por defecto, asumimos el branch_id del usuario (útil para meseros/gerentes)
    branch_id = user.get("branch_id")

    # Si el dueño/admin usa el selector del Topbar:
    branch_header = request.headers.get("X-Branch-ID")
    is_owner_or_admin = "owner" in user.get("role", "") or "admin" in user.get("role", "")
    if is_owner_or_admin:
        if branch_header and branch_header.isdigit():
            branch_id = int(branch_header)
        # branch_id=None → admin global view (all branches)
    else:
        # Non-admin: must always have a branch_id; fall back to restaurant_id if missing
        if branch_id is None:
            branch_id = user.get("restaurant_id")
        if branch_id is None:
            raise HTTPException(status_code=400, detail="No se pudo determinar la sucursal del usuario")

    with bypass_tenant_scope("get_tables: admin global view or branch-scoped via user.branch_id"):
        tables = await db.db_get_tables(branch_id=branch_id)
    return {"tables": tables}

@router.post("/api/tables")
async def create_table(request: Request):
    """Crea una mesa automáticamente sin pedir número ni nombre manual."""
    await require_auth(request)
    user = await get_current_user(request)
    restaurant = await get_current_restaurant(request)

    # Wave-2: restaurant["id"] is normalized to org_id (the tenant key, same
    # for the Matriz AND all its branches). The X-Branch-ID header carries
    # a LOCATION_ID (the sede the admin is viewing in the dropdown). These
    # are TWO DIFFERENT integers — must NOT be conflated:
    #   - tenant_scope() expects org_id (sets app.org_id GUC for RLS)
    #   - db_auto_create_table() expects the sede / branch id (used as
    #     branch_id and location_id columns on restaurant_tables)
    org_id = restaurant["id"]
    branch_location_id = restaurant.get("location_id") or org_id

    branch_header = request.headers.get("X-Branch-ID")
    if branch_header and branch_header.isdigit() and ("owner" in user.get("role", "") or "admin" in user.get("role", "")):
        candidate = int(branch_header)
        branch_rest = await db.db_get_restaurant_by_id(candidate)
        if branch_rest:
            # Header value is the location_id of the selected sede. The
            # org_id stays the same — all branches of a Matriz share one org.
            branch_location_id = candidate

    with tenant_scope(org_id):
        new_table = await db.db_auto_create_table(branch_location_id)

    return {"success": True, "table_id": new_table["id"], "name": new_table["name"]}

async def _verify_table_ownership(table_id: str, restaurant: dict) -> None:
    """Verify the table belongs to this restaurant (or its branches).

    After migration 0018, branch_id is always NOT NULL and equals the owning
    restaurant's id (parent or branch).
    """
    row = await tr.db_verify_table_in_restaurant(table_id, restaurant["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Table not found")
    rest_id = restaurant["id"]
    table_branch_id = row["branch_id"]

    # Direct ownership: table belongs to this restaurant
    if table_branch_id == rest_id:
        return
    # Parent access: if current restaurant is the parent, allow branch tables
    is_parent = restaurant.get("parent_restaurant_id") is None
    if is_parent:
        if await tr.db_verify_branch_is_child(table_branch_id, rest_id):
            return
    raise HTTPException(status_code=403, detail="Table does not belong to this restaurant")


@router.delete("/api/tables/{table_id}")
async def delete_table(table_id: str, restaurant=Depends(get_current_restaurant_scoped)):
    """Elimina una mesa por su ID."""
    await _verify_table_ownership(table_id, restaurant)
    await db.db_delete_table(table_id)
    return {"success": True}


@router.get("/api/tables/floor-plan")
async def get_floor_plan(request: Request, restaurant=Depends(get_current_restaurant_scoped)):
    """Devuelve todas las mesas con posiciones y ocupación actual para el mapa de planta."""
    branch_id_str = request.headers.get("x-branch-id")
    branch_id = int(branch_id_str) if branch_id_str and branch_id_str.isdigit() else restaurant["id"]
    return await db.db_get_floor_plan(branch_id=branch_id)


class TablePositionBody(BaseModel):
    position_x: float = Field(0, ge=-10000, le=10000)
    position_y: float = Field(0, ge=-10000, le=10000)


_VALID_TABLE_TYPES = {"interior", "terraza", "barra", "privado", "vip"}


class TablePropertiesBody(BaseModel):
    capacity: int | None = Field(None, ge=1, le=100)
    table_type: str | None = None
    zone: str | None = None


@router.put("/api/tables/{table_id}/position")
async def update_table_position(table_id: str, body: TablePositionBody, restaurant=Depends(get_current_restaurant_scoped)):
    """Actualiza la posición (x, y) de una mesa en el mapa de planta."""
    await _verify_table_ownership(table_id, restaurant)
    result = await db.db_update_table_position(
        table_id, body.position_x, body.position_y
    )
    if not result:
        return JSONResponse({"detail": "Table not found"}, status_code=404)
    return result


@router.put("/api/tables/{table_id}/properties")
async def update_table_properties(table_id: str, body: TablePropertiesBody, restaurant=Depends(get_current_restaurant_scoped)):
    """Actualiza propiedades de una mesa (capacity, table_type, zone)."""
    await _verify_table_ownership(table_id, restaurant)
    updates = body.model_dump(exclude_none=True)
    if "table_type" in updates and updates["table_type"] not in _VALID_TABLE_TYPES:
        raise HTTPException(status_code=400, detail=f"table_type must be one of: {', '.join(sorted(_VALID_TABLE_TYPES))}")
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    result = await db.db_update_table_properties(table_id, **updates)
    if not result:
        return JSONResponse({"detail": "Table not found"}, status_code=404)
    return result


@router.get("/menu", response_class=HTMLResponse)
async def menu_page_bot():
    """Sirve el catálogo para contexto delivery/recoger (?bot=NUMBER)."""
    p = STATIC / "html" / "menu.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="menu.html no encontrado en static/")
    return HTMLResponse(p.read_text(encoding="utf-8"))


@router.get("/menu/{table_id}", response_class=HTMLResponse)
async def menu_page(table_id: str):
    # Resolve catalog_v2_enabled to pick legacy vs current template.
    catalog_v2 = True  # default: serve new catalog
    try:
        with bypass_tenant_scope("menu_page: pre-resolve table tenant for template selection"):
            table = await db.db_get_table_by_id(table_id)
        if table:
            wa_number = await get_table_wa_number(table)
            restaurant = await db.db_get_restaurant_by_bot_number(wa_number) or {}
            feat = restaurant.get("features") or {}
            if isinstance(feat, str):
                import json as _json
                try:
                    feat = _json.loads(feat)
                except Exception:
                    feat = {}
            catalog_v2 = bool(feat.get("catalog_v2_enabled", True))
    except Exception:
        log.exception("menu_page.template_resolution_failed", table_id=table_id)
        # on any error keep default (True) — non-critical path (template fallback)

    html_file = "menu.html" if catalog_v2 else "menu-legacy.html"
    p = STATIC / "html" / html_file
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"{html_file} no encontrado en static/")
    return HTMLResponse(p.read_text(encoding="utf-8"))

@router.get("/api/public/menu-context/{table_id}")
async def public_menu_context(table_id: str):
    with bypass_tenant_scope("public_menu_context: pre-resolve table tenant for public menu"):
        table = await db.db_get_table_by_id(table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Mesa no encontrada")

    wa_number = await get_table_wa_number(table)
    if not wa_number:
        raise HTTPException(status_code=404, detail="Restaurante no configurado para esta mesa")
    wa_msg = f"Hola! Estoy en {table['name']}"
    wa_url = f"https://wa.me/{wa_number}?text={urllib.parse.quote(wa_msg)}"

    menu = await db.db_get_menu(wa_number) or {}
    restaurant = await db.db_get_restaurant_by_bot_number(wa_number) or {}
    if restaurant.get("id"):
        with tenant_scope(restaurant["id"]):
            availability = await db.db_get_menu_availability(restaurant["id"])
    else:
        availability = {}
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
        "locale": features.get("locale", "es-CO"),
        "currency": features.get("currency", "COP"),
        "catalog_v2_enabled": bool(features.get("catalog_v2_enabled", True)),
        "bot_visual_menu": bool(features.get("bot_visual_menu", False)),
        "bot_number": wa_number,
    }

def build_qr_html(menu_url: str, table_name: str, width: int = 300) -> str:
    return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body style='margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;'><div id='qr'></div><script>window.onload=function(){{new QRCode(document.getElementById('qr'),{{text:decodeURIComponent('{urllib.parse.quote(menu_url)}'),width:{width},height:{width},colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});}};</script></body></html>"

def _public_base_url(request: Request) -> str:
    """Return the public base URL for QR generation.
    Uses APP_DOMAIN when set (Railway/production); falls back to request.base_url for local dev."""
    if _APP_DOMAIN:
        return f"https://{_APP_DOMAIN}"
    return str(request.base_url).rstrip('/')

@router.get("/api/tables/{table_id}/qr", response_class=HTMLResponse)
async def get_table_qr(request: Request, table_id: str):
    with bypass_tenant_scope("qr_public_lookup: pre-resolve table tenant for QR"):
        table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    menu_url = f"{_public_base_url(request)}/menu/{table_id}"
    return build_qr_html(menu_url, table["name"], width=300)

@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(request: Request, table_id: str):
    with bypass_tenant_scope("qr_sheet_public_lookup: pre-resolve table tenant for QR sheet"):
        table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    menu_url = f"{_public_base_url(request)}/menu/{table_id}"
    encoded = urllib.parse.quote(menu_url)
    safe_name = _html.escape(table['name'])
    return HTMLResponse(
        f"<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'><style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{font-family:Arial,sans-serif;background:#fff;}}.page{{width:10cm;margin:1cm auto;text-align:center;padding:1.5cm;border:2px solid #0D1412;border-radius:16px;}}.logo{{font-size:28px;font-weight:900;color:#0D1412;margin-bottom:4px;}}.logo span{{color:#1D9E75;}}.tname{{font-size:20px;font-weight:700;color:#0D1412;margin:12px 0 4px;}}.instr{{font-size:13px;color:#666;margin-bottom:16px;line-height:1.5;}}.qrbox{{width:200px;height:200px;margin:0 auto 16px;}}.qrbox canvas,.qrbox img{{width:200px !important;height:200px !important;border-radius:8px;}}.wa-badge{{display:inline-flex;align-items:center;gap:6px;background:#25D366;color:white;padding:8px 16px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:16px;}}.steps{{text-align:left;background:#f8f8f5;border-radius:10px;padding:12px 16px;margin-top:8px;}}.step{{font-size:12px;color:#444;padding:3px 0;display:flex;gap:8px;}}.sn{{color:#1D9E75;font-weight:700;}}@media print{{body{{margin:0;}}}}</style><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body><div class='page'><div class='logo'>Mesio<span>.</span></div><div class='tname'>{safe_name}</div><div class='instr'>Escanea el QR para ver el menú<br>y pedir por WhatsApp</div><div class='qrbox' id='qrc'></div><div class='wa-badge'>Ver Menú y Pedir</div><div class='steps'><div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div><div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div><div class='step'><span class='sn'>3.</span><span>Revisa el menú interactivo</span></div><div class='step'><span class='sn'>4.</span><span>Toca pedir por WhatsApp</span></div></div></div><script>window.onload=function(){{new QRCode(document.getElementById('qrc'),{{text:decodeURIComponent('{encoded}'),width:200,height:200,colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});setTimeout(function(){{window.print();}},800);}};</script></body></html>"
    )

# ── ALERTAS MESERO ──────────────────────────────────────────────────
@router.get("/api/waiter-alerts")
async def get_waiter_alerts(request: Request):
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant.get("whatsapp_number", "")
    try:
        with tenant_scope(restaurant["id"]):
            alerts = await tr.db_get_waiter_alerts(bot_number)
    except Exception as e:
        log.exception("tables.alerts_read_failed", restaurant_id=restaurant.get("id"), error=str(e))
        alerts = []
    return {"alerts": alerts}

class AdminCallRequest(BaseModel):
    phone: str = ""
    table_id: str = ""
    table_name: str = ""
    bot_number: str = ""

@router.post("/api/waiter-alerts/admin-call")
async def admin_call_waiter(request: Request, body: AdminCallRequest):
    """El administrador convoca a un mesero/empleado a caja o dashboard."""
    await require_auth(request)
    with bypass_tenant_scope("admin_call_waiter: cross-tenant waiter alert from dashboard"):
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
    try:
        with bypass_tenant_scope("dismiss_waiter_alert: global kitchen alert dismiss"):
            await tr.db_dismiss_waiter_alert(alert_id)
    except Exception:
        pass
    return {"success": True}

# ── ELIMINAR CONVERSACIONES (MANUAL) ─────────────────────────────────
@router.delete("/api/conversations/{phone}")
async def force_delete_conversation(request: Request, phone: str):
    """Permite al mesero limpiar un chat manualmente (ej. pruebas atascadas)"""
    username = await require_auth(request)
    try:
        with bypass_tenant_scope("force_delete_conversation: manual cleanup by staff"):
            await tr.db_force_delete_conversation_data(phone, username)
    except Exception as e:
        log.error("tables.chat_cleanup_failed", error=str(e))
    return {"success": True}

# ── DELIVERY ORDERS ───────────────────────────────────────────────────
@router.get("/api/kitchen/delivery-orders")
async def get_delivery_orders(request: Request):
    await require_auth(request)
    import json as _json

    rows = await tr.db_get_delivery_orders_for_caja()
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
    rows = await tr.db_get_delivery_status_hash()
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
        
    with bypass_tenant_scope("update_delivery_order_status: delivery order by ID"):
        await tr.db_update_delivery_order_status(order_id, new_status)

    if new_status in ("confirmado", "en_camino", "entregado", "listo"):
        with bypass_tenant_scope("update_delivery_order_status: contact lookup by order ID"):
            row = await tr.db_get_delivery_order_contact(order_id)
            full = await tr.db_get_delivery_order_full(order_id) if new_status == "listo" else None
        if row:
            phone = row["phone"]
            order_type = (full or {}).get("order_type", "domicilio")
            if new_status == "confirmado":
                msg = f"✅ ¡Tu pedido fue confirmado! Ya está en preparación y pronto estará listo. 🍽️"
            elif new_status == "listo" and order_type == "recoger":
                msg = "🛍️ ¡Tu pedido está listo para recoger! Puedes pasar a buscarlo cuando quieras. ¡Te esperamos!"
            elif new_status == "en_camino":
                msg = f"🛵 ¡Tu pedido ya va en camino a {row['address']}! Pronto estaremos contigo."
            elif new_status == "entregado":
                msg = f"✅ ¡Tu pedido fue entregado! Total: ${int(row['total']):,} COP. ¡Gracias por tu compra!"
            else:
                msg = None
            if msg:
                try:
                    with bypass_tenant_scope("update_delivery_order_status: meta phone ID lookup"):
                        db_phone_id = await tr.db_get_meta_phone_id_for_session(phone)
                except Exception:
                    db_phone_id = None
                await send_wa_msg(phone, msg, db_phone_id)

    if new_status == "confirmado":
        with bypass_tenant_scope("update_delivery_order_status: full order for billing"):
            order_row = await tr.db_get_delivery_order_full(order_id)
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
                    "total": float(to_decimal(order_row["total"])),       # JSON boundary
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

    # Resolve effective branch_id: from header, user, or staff context
    effective_bid = branch_id or user.get("restaurant_id")
    with bypass_tenant_scope("get_table_orders: may span branches or be admin view"):
        rows = await tr.db_get_table_orders_for_branch(
            branch_id=effective_bid, status=status, is_admin=is_admin
        )

    import json as _json
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get('items'), str):
            try: d['items'] = _json.loads(d['items'])
            except (ValueError, TypeError): pass
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

    with bypass_tenant_scope("get_order_ticket: ticket lookup by order_id across branches"):
        rows = await tr.db_get_table_orders_by_base_id(order_id, branch_id)

    if not rows:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    # Agregar ítems y totales de todas las sub-órdenes
    all_items: list = []
    total: Decimal = Decimal("0")
    notes_parts: list = []
    first = rows[0]

    for row in rows:
        items = row.get("items", [])
        if isinstance(items, str):
            try:
                items = _json.loads(items)
            except Exception:
                items = []
        if isinstance(items, list):
            all_items.extend(items)
        total += to_decimal(row.get("total") or 0)
        if row.get("notes"):
            notes_parts.append(row["notes"])

    # Datos fiscales: última factura emitida para esta orden (deferred to billing layer)
    fiscal = None
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
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
                log.info("tables.wa_notification_sent", phone=phone, status=resp.status_code)
        except Exception as e:
            log.error("tables.wa_notification_failed", phone=phone, error=str(e))
    else:
        log.warning("tables.wa_notification_skipped_no_credentials", phone=phone, has_token=bool(token), phone_id=final_phone_id)


async def send_wa_interactive_nps(phone: str, nps_label: str, db_phone_id: str = None):
    """Send the NPS rating question as an interactive WhatsApp message with a skip button."""
    token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")
    final_phone_id = db_phone_id or os.getenv("META_PHONE_NUMBER_ID") or os.getenv("WHATSAPP_PHONE_ID", "")

    if not token or not final_phone_id:
        log.warning("tables.nps_interactive_skipped_no_credentials", phone=phone)
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
            log.info("tables.nps_interactive_sent", phone=phone, status=resp.status_code)
    except Exception as e:
        log.error("tables.nps_interactive_failed", phone=phone, error=str(e))

@router.post("/api/table-orders/{order_id}/status")
async def update_order_status(request: Request, order_id: str):
    username = await require_auth(request)
    user = await get_current_user(request)
    body = await request.json()
    status = body.get("status")

    valid_statuses = list(_STATUS_ROLE_MAP.keys())
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Estado inválido")

    # Role-based transition guard
    user_roles = {r.strip() for r in user.get("role", "").split(",") if r.strip()}
    allowed_roles = _STATUS_ROLE_MAP.get(status, set())
    if not user_roles.intersection(allowed_roles):
        raise HTTPException(status_code=403, detail=f"Tu rol no puede cambiar el estado a '{status}'")
    
    with bypass_tenant_scope("update_order_status: order lookup by ID across branches"):
        order_record = await tr.db_get_table_order_record(order_id)
    if not order_record:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    order = order_record
    phone = order.get("phone")
    table_name = order.get("table_name", "tu mesa")

    db_phone_id = None
    session_data = None
    if phone and phone != "manual":
        try:
            with bypass_tenant_scope("update_order_status: session lookup by phone"):
                session = await tr.db_get_open_table_session_by_phone(phone)
            if session:
                session_data = session
                db_phone_id = session_data.get("meta_phone_id")
        except Exception:
            log.exception("tables.session_lookup_error", phone=phone)

    if status == "generar_factura":
        base_id = order.get("base_order_id") or order_id
        with bypass_tenant_scope("update_order_status: mark factura generada by order ID"):
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
        with bypass_tenant_scope("update_order_status: close table bill by order ID"):
            await db.db_close_table_bill(base_id)
        if phone and phone != "manual":
            await _farewell_and_nps(phone, order.get("table_id"), session_data, db_phone_id, username)
        return {"success": True, "order_id": order_id, "status": "factura_entregada"}

    # ── C. ESTADOS NORMALES (Prep, Listo, Entregado) ──
    else:
        with bypass_tenant_scope("update_order_status: normal status update by order ID"):
            await db.db_update_table_order_status(order_id, status)
        # bot_number needed for per-tenant rate-limit key (Redis, cross-worker safe)
        _bot_number = (session_data.get("bot_number") if session_data else None) or order.get("bot_number", "")
        if status == "entregado" and phone and phone != "manual":
            _rl_key = f"notif_wa:{_bot_number}:{phone}:entregado"
            if await state_store.rate_limit_check(_rl_key, max_requests=1, window_seconds=300):
                msg = f"¡Tu pedido ha llegado a {table_name}! 🍽️\n\n¡Que lo disfrutes! Cuando estés listo, puedes pedir la cuenta aquí mismo."
                await send_wa_msg(phone, msg, db_phone_id)
        if status == "listo" and phone and phone != "manual":
            _rl_key = f"notif_wa:{_bot_number}:{phone}:listo"
            if await state_store.rate_limit_check(_rl_key, max_requests=1, window_seconds=300):
                msg = f"🍽️ ¡Tu pedido en {table_name} está listo!\n\nUn mesero te lo llevará en un momento. ¡Buen provecho! 😋"
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
    total:      Decimal
    notes:      str = ""
    station:    str = "all"
    branch_id:  int = None  # 🛡️ Agregamos branch_id al modelo
    
@router.get("/api/pos/menu")
async def get_pos_menu(request: Request):
    """Devuelve el menú del restaurante para pintarlo en el POS del mesero.

    Wave-2: the menu lives at the org level (organizations.menu). The wa_number
    used for the menu lookup must come from the staff's actual sede (resolved
    via user.branch_id → location.whatsapp_number); we no longer fall back
    to "any restaurant globally" — that would render another customer's menu
    in this customer's POS (cross-tenant leak).
    """
    user = await get_current_user(request)

    wa_number = ""
    if user and user.get("branch_id"):
        r = await db.db_get_restaurant_by_id(user["branch_id"])
        if r:
            wa_number = r.get("whatsapp_number", "") or ""

    if not wa_number:
        # Cannot resolve the staff's sede → return empty menu rather than a
        # cross-tenant one. The frontend handles {} gracefully (shows
        # "menu not configured" state) instead of mixing data from another tenant.
        return {"menu": {}}

    menu = await db.db_get_menu(wa_number) or {}
    return {"menu": menu}

@router.get("/api/pos/tables-status")
async def get_tables_status(request: Request):
    """Devuelve todas las mesas y su estado actual (ideal para pintar el mapa)"""
    await require_auth(request)

    # 1. Resolución de contexto inteligente
    restaurant = await get_current_restaurant(request)

    # Wave-2 model: every restaurant is a `locations` row. There is NO special
    # "matriz" entity at the data layer — `is_primary=true` simply marks the
    # primary sede of an org. We need TWO distinct integers here:
    #   - org_id        : tenant key for tenant_scope() / RLS GUC
    #   - location_id   : the sede id stored in restaurant_tables.branch_id
    # Both are consistently populated by db_get_restaurant_by_id (and now also
    # by db_get_all_restaurants post the same-paso fix). If location_id is
    # missing we fail fast — silently falling back to org_id (the old
    # "Matriz invariant" trick) only works for orgs created BEFORE Wave-2 deploy
    # where 0034 backfilled org_id == matriz_location_id by coincidence.
    org_id = restaurant["id"]
    location_id = restaurant.get("location_id")
    if location_id is None:
        raise HTTPException(
            status_code=500,
            detail="Restaurant context missing location_id — cannot resolve sede",
        )

    with tenant_scope(org_id):
        tables = await db.db_get_tables(branch_id=location_id)
        pending_orders = await tr.db_get_pending_orders_by_branch(location_id)

    # db_get_active_session_table_ids uses bypass internally (cross-tenant)
    session_map = await tr.db_get_active_session_table_ids()

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

    with bypass_tenant_scope("adjust_table_bill: lookup by order ID, branch resolved upstream"):
        found = await tr.db_adjust_table_bill(base_order_id, adjusted_items, new_total)
    if not found:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    log.info("tables.invoice_adjusted", base_order_id=base_order_id, new_total=str(new_total))
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
    total_d = quantize_money(to_decimal(body.total))

    with bypass_tenant_scope("pos_manual_order: branch scoped via body.branch_id"):
        base_id = await db.db_get_base_order_id(body.table_id)

        if base_id:
            final_base_id = base_id
            sub_num = await db.db_get_next_sub_number(base_id)
        else:
            final_base_id = order_id
            sub_num = 1

        # Resolve waiter_staff_id from the authenticated user if available.
        _waiter_staff_id = user.get("staff_id") or None

        order = {
            "id":              order_id,
            "table_id":        body.table_id,
            "table_name":      body.table_name,
            "phone":           phone,
            "items":           body.items,
            "status":          "recibido",
            "notes":           body.notes,
            "total":           float(total_d),  # JSON boundary
            "base_order_id":   final_base_id,
            "sub_number":      sub_num,
            "station":         body.station,
            "branch_id":       branch_id,
            "channel":         "pos",
            "waiter_staff_id": _waiter_staff_id,
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
    # First try with the user's branch filter; if nothing found (e.g. Matriz admin
    # handling a branch order), retry without the branch filter. The ownership
    # check below still enforces restaurant boundaries.
    with bypass_tenant_scope("create_checks: ticket lookup by order ID across branches"):
        ticket = await db.db_get_order_ticket_data(base_order_id, user.get("branch_id") or None)
        if not ticket:
            ticket = await db.db_get_order_ticket_data(base_order_id, None)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")

    # Mapa de qty disponible por plato en el ticket original
    available: dict[str, int] = {}
    for item in ticket.get("items", []):
        key = item["name"].strip().lower()
        available[key] = available.get(key, 0) + int(item.get("quantity", item.get("qty", 1)))

    # Ownership check: ticket must belong to this user's org (Wave-2 tenant boundary)
    ticket_org_id = ticket.get("org_id")
    user_branch_id = user.get("branch_id") or user.get("restaurant_id")
    user_org_id = None
    if user_branch_id:
        with bypass_tenant_scope("create_checks: resolve user org_id from branch_id"):
            user_rest = await db.db_get_restaurant_by_id(int(user_branch_id))
        if user_rest:
            user_org_id = user_rest.get("org_id")
    # Fail closed: if either side is unresolvable, deny rather than allow cross-tenant write
    if ticket_org_id is None or user_org_id is None or ticket_org_id != user_org_id:
        raise HTTPException(status_code=403, detail="Este ticket no pertenece a tu organización")

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

    # Validar que el desglose cubre TODOS los ítems del ticket (no solo que no exceda)
    for name, avail_qty in available.items():
        assigned = check_totals.get(name, 0)
        if assigned < avail_qty:
            raise HTTPException(
                status_code=400,
                detail=f"El desglose no cubre todos los ítems. Faltan: {name} x{avail_qty - assigned}"
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

    with bypass_tenant_scope("create_checks: write split checks by order ID"):
        result = await db.db_create_checks(base_order_id, validated)
    return {"success": True, "checks": result}


@router.get("/api/table-orders/{base_order_id}/checks")
async def get_checks(request: Request, base_order_id: str):
    """Lista todos los checks de una mesa con sus datos fiscales."""
    await get_current_user(request)
    with bypass_tenant_scope("get_checks: checks lookup by order ID across branches"):
        checks = await db.db_get_checks(base_order_id)
    return {"checks": checks}

@router.post("/api/table-orders/{base_order_id}/checks/{check_id}/pay")
async def pay_check(request: Request, base_order_id: str, check_id: str, body: PayCheckBody):
    try:
        # Rate limit: 3 payments per check per 10 seconds (prevents double-click)
        from app.services import state_store
        rl_key = f"pay:{check_id}"
        if not await state_store.rate_limit_check(rl_key, max_requests=3, window_seconds=10):
            raise HTTPException(status_code=429, detail="Demasiadas solicitudes de pago. Intenta de nuevo en unos segundos.")
        restaurant = await get_current_restaurant(request)
        with tenant_scope(restaurant["id"]):
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
        tip_cap_base = to_decimal(check["total"]) + to_decimal(body.service_charge)
        if tip_amount_d > 0 and tip_amount_d > money_mul(tip_cap_base, Decimal("0.5")):
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

        with tenant_scope(restaurant["id"]):
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
            log.warning("tables.loyalty_accrue_not_implemented", check_id=check_id)

        with tenant_scope(restaurant["id"]):
            order_row = await db.db_get_first_table_order(base_order_id)
        if order_row and order_row["status"] == "factura_entregada":
            customer_phone = order_row.get("phone")
            if customer_phone and customer_phone != "manual":
                with tenant_scope(restaurant["id"]):
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
    with bypass_tenant_scope("attach_proof: proof attachment by order ID across branches"):
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

    with tenant_scope(restaurant["id"]):
        proposals = await db.db_list_checkout_proposals(restaurant["id"], branch_ids)
    return {"proposals": proposals}


@router.delete("/api/checkout-proposals/{base_order_id}")
async def cancel_checkout_proposal(base_order_id: str, request: Request):
    restaurant = await get_current_restaurant(request)  # auth check
    with tenant_scope(restaurant["id"]):
        await db.db_cancel_checkout_proposal(base_order_id)
    return {"success": True}


@router.get("/api/table-orders/{base_order_id}/checks/{check_id}/ticket")
async def get_check_ticket(request: Request, base_order_id: str, check_id: str):
    """Devuelve los datos del check para impresión de factura térmica."""
    await get_current_user(request)
    with bypass_tenant_scope("get_check_ticket: ticket lookup by check ID across branches"):
        ticket = await db.db_get_check_ticket(check_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Check no encontrado")
    return ticket


@router.delete("/api/table-orders/{base_order_id}/checks/{check_id}")
async def delete_check(request: Request, base_order_id: str, check_id: str):
    """Elimina un check en estado 'open'. No afecta checks ya cobrados."""
    await get_current_user(request)
    with bypass_tenant_scope("delete_check: check deletion by ID across branches"):
        deleted = await db.db_delete_open_check(check_id)
    if not deleted:
        raise HTTPException(
            status_code=400,
            detail="No se puede eliminar: el check no existe o ya fue procesado"
        )
    return {"success": True}


class QuickInvoiceItem(BaseModel):
    name: str
    qty: int = 1
    unit_price: Decimal

class QuickInvoiceBody(BaseModel):
    items: list[QuickInvoiceItem]
    tip_amount: float = Field(0.0, ge=0.0)
    payment_method: str = "efectivo"
    customer_name: str = "Consumidor Final"
    customer_nit: str = "222222222"
    customer_email: str = ""
    order_type: str = "salon"     # salon | domicilio
    table_name: str = "Caja"
    branch_id: int | None = None


@router.post("/api/pos/quick-invoice")
async def pos_quick_invoice(request: Request, body: QuickInvoiceBody):
    """
    Crea una venta rápida desde caja sin pasar por el flujo de mesa/bot.
    Crea un table_order efímero, un check y lo paga en un solo paso.
    """
    restaurant = await get_current_restaurant(request)
    user = await get_current_user(request)

    branch_id = body.branch_id or user.get("branch_id") or restaurant["id"]

    if not body.items:
        raise HTTPException(status_code=400, detail="Se requiere al menos un ítem")

    subtotal = quantize_money(money_sum(money_mul(to_decimal(it.unit_price), it.qty) for it in body.items))
    tip_d = to_decimal(body.tip_amount)
    total_d = quantize_money(subtotal + tip_d)

    if tip_d > 0 and tip_d > money_mul(subtotal, Decimal("0.5")):
        raise HTTPException(status_code=400, detail="La propina no puede superar el 50% del subtotal")

    order_id = f"qi-{str(uuid.uuid4())[:8]}"
    base_order_id = order_id

    items_payload = [
        {"name": it.name, "quantity": it.qty,
         "price": float(quantize_money(to_decimal(it.unit_price))),         # JSON boundary
         "subtotal": float(money_mul(to_decimal(it.unit_price), it.qty))}   # JSON boundary
        for it in body.items
    ]

    order = {
        "id": order_id,
        "table_id": None,
        "table_name": body.table_name,
        "phone": "caja",
        "items": items_payload,
        "status": "recibido",
        "notes": f"Factura rápida ({body.order_type})",
        "total": float(total_d),
        "base_order_id": base_order_id,
        "sub_number": 1,
        "station": "all",
        "branch_id": branch_id,
        "channel": "pos",
        "waiter_staff_id": user.get("staff_id") or None,
    }
    with tenant_scope(restaurant["id"]):
        await db.db_save_table_order(order)

        # Crear un check único para esta venta
        check_payload = [{
            "check_number": 1,
            "items": items_payload,
            "subtotal": float(to_decimal(subtotal)),
            "tax_amount": 0.0,
            "total": float(to_decimal(subtotal)),
        }]
        created = await db.db_create_checks(base_order_id, check_payload)
    if not created:
        raise HTTPException(status_code=500, detail="No se pudo crear el check")
    check_id = created[0]["id"]

    # Billing / DIAN (opcional)
    features = restaurant.get("features") or {}
    if isinstance(features, str):
        import json as _json
        try:
            features = _json.loads(features)
        except Exception:
            features = {}
    _currency = features.get("currency") if isinstance(features, dict) else None

    raw_dian = features.get("dian_active", False)
    if isinstance(raw_dian, str):
        dian_active = raw_dian.strip().lower() in ("true", "1", "yes", "on")
    else:
        dian_active = bool(raw_dian)

    fiscal_invoice_id = None
    if dian_active:
        config = await billing.get_billing_config(restaurant["id"])
        if config:
            config["_restaurant_id"] = restaurant["id"]
            provider = config.get("provider", "mesio_native")
            adapter = billing.get_adapter(provider)
            order_for_billing = {
                "id": check_id,
                "total": float(total_d),
                "subtotal": float(to_decimal(subtotal)),
                "service_charge": 0.0,
                "items": items_payload,
                "payment_method": body.payment_method,
                "order_ref": base_order_id,
                "customer": {
                    "name": body.customer_name,
                    "nit": body.customer_nit,
                    "email": body.customer_email,
                },
            }
            try:
                fiscal = await adapter.create_invoice(order_for_billing, config)
                fiscal_invoice_id = fiscal["id"]
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Error al emitir factura DIAN: {exc}")

    payments_list = [{"method": body.payment_method, "amount": float(total_d)}]

    with tenant_scope(restaurant["id"]):
        await db.db_finalize_check_payment(
            check_id=check_id,
            base_order_id=base_order_id,
            payments=payments_list,
            change_amount=0.0,
            fiscal_invoice_id=fiscal_invoice_id,
            customer_name=body.customer_name,
            customer_nit=body.customer_nit,
            customer_email=body.customer_email,
            tip_amount=float(tip_d),
        )

    return {
        "success": True,
        "order_id": order_id,
        "check_id": check_id,
        "total": float(total_d),
        "fiscal_invoice_id": fiscal_invoice_id,
    }


# ── CAJA: Customer lookup ─────────────────────────────────────────────────────

@router.get("/api/caja/customer/{phone}")
async def get_caja_customer(
    phone: str,
    restaurant: dict = Depends(get_current_restaurant_scoped),
) -> dict:
    """Return customer profile + loyalty balance + recent orders for the caja UI.

    Auth: Bearer token of admin/owner/gerente (get_current_restaurant_scoped).
    Tenant-scoped: all repo calls run under tenant_scope(org_id) set by the dep.

    Returns:
        {
            "phone": str,
            "name": str | None,
            "is_known": bool,
            "stats": {"total_orders", "total_spent", "last_seen", "first_seen"} | {},
            "loyalty": {"points": int, "tier": null} | null,
            "recent_orders": [{"id", "total", "created_at", "items_summary"}]
        }

    If the phone is unknown, is_known=false with empty stats and empty recent_orders.
    If the loyalty module is disabled or has no record, loyalty=null.
    """
    from app.repositories import customer_profiles_repo as cp_repo  # noqa: PLC0415
    from app.repositories import loyalty_repo  # noqa: PLC0415
    from app.services.money import quantize_money, to_decimal  # noqa: PLC0415

    # Normalise phone: strip leading +, spaces, and URL-encode artifacts.
    # Keep the original version for display but normalise for DB lookup.
    clean_phone = urllib.parse.unquote(phone).strip()

    # Resolve the real org_id.
    # restaurant["id"] == location_id (restaurants VIEW id column) — DO NOT use it as org_id.
    # db_get_restaurant_by_id populates org_id on the dict; use it directly when present.
    # For any call site that provides a restaurant dict without org_id, fall back to a
    # single-query lookup via the location_id.
    if restaurant.get("org_id"):
        org_id: int = int(restaurant["org_id"])
    else:
        from app.repositories.restaurant_repo import db_resolve_org_id_from_location  # noqa: PLC0415
        org_id_resolved = await db_resolve_org_id_from_location(int(restaurant["id"]))
        if org_id_resolved is None:
            return {"phone": clean_phone, "name": None, "is_known": False,
                    "stats": {}, "loyalty": None, "recent_orders": []}
        org_id = org_id_resolved  # explicit org_id resolved from location — DO NOT use restaurant["id"]

    # ── Customer profile ──────────────────────────────────────────────────────
    profile = await cp_repo.get_profile(org_id, clean_phone)

    if profile is None:
        return {
            "phone": clean_phone,
            "name": None,
            "is_known": False,
            "stats": {},
            "loyalty": None,
            "recent_orders": [],
        }

    # ── Loyalty balance (best-effort — module may be disabled) ───────────────
    loyalty_data: dict | None = None
    try:
        lb = await loyalty_repo.db_get_loyalty_balance(org_id, clean_phone)
        if lb is not None:
            loyalty_data = {
                "points": lb.get("puntos_actuales", 0),
                "tier": None,
            }
    except Exception:
        log.exception("caja_customer.loyalty_lookup_failed", phone=clean_phone, org_id=org_id)

    # ── Recent orders (last 5 from orders + table_orders, by phone) ──────────
    recent_orders: list[dict] = await _get_recent_orders_for_phone(org_id, clean_phone, limit=5)

    return {
        "phone": clean_phone,
        "name": profile.get("display_name"),
        "is_known": True,
        "stats": {
            "total_orders": profile.get("total_orders") or 0,
            "total_spent": float(quantize_money(to_decimal(profile.get("total_spent") or 0))),  # JSON boundary
            "last_seen": profile.get("last_seen").isoformat() if profile.get("last_seen") else None,
            "first_seen": profile.get("first_seen").isoformat() if profile.get("first_seen") else None,
        },
        "loyalty": loyalty_data,
        "recent_orders": recent_orders,
    }


async def _get_recent_orders_for_phone(org_id: int, phone: str, limit: int = 5) -> list[dict]:
    """Orchestrate recent delivery + table orders for a phone; sort and cap.

    SQL lives in the repos (orders_repo / tables_repo). This is a pure orchestrator.
    Runs under the already-active tenant_scope set by get_current_restaurant_scoped.
    """
    from app.repositories.orders_repo import db_get_recent_orders_by_phone  # noqa: PLC0415
    from app.repositories.tables_repo import db_get_recent_table_orders_by_phone  # noqa: PLC0415

    try:
        delivery = await db_get_recent_orders_by_phone(org_id, phone, limit)
        table = await db_get_recent_table_orders_by_phone(org_id, phone, limit)
    except Exception:
        log.exception("caja_customer.recent_orders_failed", phone=phone, org_id=org_id)
        return []

    combined = delivery + table
    combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return combined[:limit]