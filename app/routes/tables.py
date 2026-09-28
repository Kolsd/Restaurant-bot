import asyncio
import html as _html
import os
import urllib.parse
import uuid
from decimal import Decimal
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from app.services import database as db
from app.services import billing
from app.services import realtime
from app.services import sede_menu
from app.services import state_store
from app.services.agent import trigger_nps
from app.routes.deps import (
    require_auth, get_current_user, get_current_restaurant,
    get_current_restaurant_scoped, may_span_locations, resolve_sede_filter,
)
from app.services.tenant_context import tenant_scope, bypass_tenant_scope
from app.services.tenant_db import tenant_connection
from app.services import loyalty as loyalty_svc
from app.services.money import to_decimal, money_mul, quantize_money, money_sum
from app.services.logging import get_logger
from app.repositories import delivery_repo
from app.repositories import tables_repo as tr

log = get_logger(__name__)

router = APIRouter()
_APP_DOMAIN = os.getenv("APP_DOMAIN", "")

# Role-based status transition map: which roles may set each status
_STATUS_ROLE_MAP: dict[str, set[str]] = {
    'recibido':         {'cocina', 'bar', 'caja', 'mesero', 'admin', 'owner', 'gerente'},
    'en_preparacion':   {'cocina', 'bar', 'admin', 'owner', 'gerente'},
    'listo':            {'cocina', 'bar', 'admin', 'owner', 'gerente'},
    'entregado':        {'mesero', 'caja', 'admin', 'owner', 'gerente'},
    'generar_factura':  {'caja', 'admin', 'owner', 'gerente', 'mesero'},
    'cerrar_mesa':      {'caja', 'admin', 'owner', 'gerente'},
    'factura_entregada':{'caja', 'admin', 'owner', 'gerente'},
    'cancelado':        {'caja', 'mesero', 'admin', 'owner', 'gerente'},
}

# WA notification rate-limiting moved to Redis via state_store (multi-worker safe).
# Keys: notif_wa:{bot_number}:{phone}:{kind}  max 1 per 5 min per worker pool.


async def _get_restaurant_for_table(table_id: str | None, session_data: dict | None) -> dict:
    """Resuelve el restaurante/sucursal a partir de la mesa o la sesión activa."""
    if table_id:
        with bypass_tenant_scope("_get_restaurant_for_table: table lookup by ID"):
            table = await db.db_get_table_by_id(table_id)
        if table:
            bid = table.get("branch_id")
            if bid:
                r = await db.db_get_restaurant_by_location_id(bid)
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

async def _farewell_and_nps(phone: str, table_id: str | None, session_data: dict | None, username: str) -> None:
    rest = await _get_restaurant_for_table(table_id, session_data)
    final_bot_num = (
        (session_data.get("bot_number") if session_data else None)
        or rest.get("whatsapp_number") or ""  # the org's bot key
    )

    rest_name = rest.get("name", "nuestro restaurante")
    # The diner answers the survey in their own chat: GET /api/diner/status
    # renders the nps_prompt block (blocks.py) once trigger_nps sets the state.
    # Trigger the NPS survey directly
    if final_bot_num:
        asyncio.create_task(trigger_nps(phone, final_bot_num, rest_name))
        with bypass_tenant_scope("farewell_and_nps: mark session nps_pending by phone"):
            await db.db_mark_session_nps_pending(phone, final_bot_num)

    with bypass_tenant_scope("farewell_and_nps: cleanup checkout data by phone"):
        await db.db_cleanup_after_checkout(phone)

# ── TABLES ────────────────────────────────────────────────────────────

async def _tables_scope(request: Request) -> tuple[int, int | None]:
    """(org_id, branch_id) for a table listing, verified against the user.

    P0 fix (2026-09): previously used user["branch_id"] (mixed id kind —
    for staff it is actually the ORG id, not a location id) directly as
    the location_id filter, under bypass_tenant_scope (no RLS net). If
    that number happened to collide with an unrelated org's real location
    id, this leaked that org's tables. Now scoped by the explicit org_id
    under REAL tenant_scope (RLS-protected), and X-Branch-ID is verified
    to belong to that org before being used as a location filter.

    Shared by the table list and the QR sheet so the codes a restaurant
    prints can never cover a sede its listing does not show.
    """
    user = await get_current_user(request)

    org_id = user.get("org_id")
    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo determinar la organización del usuario")
    org_id = int(org_id)

    is_owner_or_admin = "owner" in user.get("role", "") or "admin" in user.get("role", "")
    branch_header = request.headers.get("X-Branch-ID")
    branch_id = None
    if is_owner_or_admin:
        if branch_header and branch_header.isdigit():
            candidate = int(branch_header)
            candidate_rest = await db.db_get_restaurant_by_location_id(candidate)
            if not candidate_rest or candidate_rest.get("org_id") != org_id:
                raise HTTPException(status_code=403, detail="Sucursal no pertenece a tu organización")
            branch_id = candidate
        # branch_id=None → admin global view (all branches of this org)
    else:
        # Non-admin (mesero/gerente/etc): staff has no per-location
        # assignment today, so they see every table of their own org.
        branch_id = user.get("location_id")

    return org_id, branch_id


@router.get("/api/tables")
async def get_tables(request: Request):
    """Returns the current branch's tables for rendering on the dashboard."""
    await require_auth(request)
    org_id, branch_id = await _tables_scope(request)
    with tenant_scope(org_id):
        tables = await db.db_get_tables(branch_id=branch_id)
    return {"tables": tables}

@router.post("/api/tables")
async def create_table(request: Request):
    """Automatically creates a table without asking for a manual number or name."""
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
        # P0 fix (2026-09): verify the candidate location actually belongs to
        # the caller's own org before trusting it — previously ANY existing
        # location id was accepted with no ownership check, letting an
        # owner/admin create a table under a DIFFERENT tenant's location id
        # while scoped under their own org (cross-tenant corruption).
        branch_rest = await db.db_get_restaurant_by_location_id(candidate)
        if branch_rest and branch_rest.get("org_id") == org_id:
            # Header value is the location_id of the selected sede. The
            # org_id stays the same — all branches of a Matriz share one org.
            branch_location_id = candidate

    with tenant_scope(org_id):
        new_table = await db.db_auto_create_table(branch_location_id)

    return {"success": True, "table_id": new_table["id"], "name": new_table["name"]}

async def _verify_table_ownership(table_id: str, restaurant: dict) -> None:
    """Verify the table belongs to this restaurant's org (Wave-2 semantics).

    Wave-2: parent_restaurant_id column dropped in 0038.  Ownership is now
    determined by org_id.  A table belongs to the caller's org when the
    table's branch_id resolves to the same org_id as the caller's restaurant.
    """
    row = await tr.db_verify_table_in_restaurant(table_id, restaurant["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Table not found")

    org_id = restaurant["id"]  # post-Wave-2: restaurant["id"] == org_id
    table_branch_id = row["branch_id"]

    # Direct match: table's branch is within this org (most common path)
    if table_branch_id == org_id:
        return

    # Cross-location check: verify the table's branch belongs to the same org
    branch_rest = await db.db_get_restaurant_by_location_id(table_branch_id)
    if branch_rest and branch_rest.get("org_id") == org_id:
        return

    raise HTTPException(status_code=403, detail="Table does not belong to this restaurant")


@router.delete("/api/tables/{table_id}")
async def delete_table(table_id: str, restaurant=Depends(get_current_restaurant_scoped)):
    """Deletes a table by its ID."""
    await _verify_table_ownership(table_id, restaurant)
    await db.db_delete_table(table_id)
    return {"success": True}


@router.get("/api/tables/floor-plan")
async def get_floor_plan(request: Request, restaurant=Depends(get_current_restaurant_scoped)):
    """Returns all tables with positions and current occupancy for the floor plan.

    Filtering:
      - X-Branch-ID = digit (location_id) → filter to that sede.
      - X-Branch-ID = 'matriz' / 'all' / absent → no sede filter; RLS
        (ENABLE+FORCE ROW LEVEL SECURITY on restaurant_tables, scoped by
        org_id from get_current_restaurant_scoped) returns every table of
        the current org across all sedes.

    Pre-2026-04-29 the fallback was restaurant["id"], which post-Wave-2
    is org_id. After migration 0057 (sync_table_branch_id) tables carry
    branch_id == location_id, so filtering "branch_id = org_id" matched
    nothing for any owner whose branch dropdown was on Casa Matriz —
    floor plan went empty for everyone.
    """
    # The header alone used to decide this, with no role check: a waiter of
    # sede A could name sede B, and one who named nothing got the floor plan
    # of every sede in the org.
    user = await get_current_user(request)
    branch_id = resolve_sede_filter(request, user)
    return await db.db_get_floor_plan(branch_id=branch_id)


class TablePositionBody(BaseModel):
    position_x: float = Field(0, ge=-10000, le=10000)
    position_y: float = Field(0, ge=-10000, le=10000)


_VALID_TABLE_TYPES = {"interior", "terraza", "barra", "privado", "vip"}


class TablePropertiesBody(BaseModel):
    name: str | None = Field(None, max_length=60)
    capacity: int | None = Field(None, ge=1, le=100)
    table_type: str | None = None
    zone: str | None = None


@router.put("/api/tables/{table_id}/position")
async def update_table_position(table_id: str, body: TablePositionBody, restaurant=Depends(get_current_restaurant_scoped)):
    """Updates a table's (x, y) position on the floor plan."""
    await _verify_table_ownership(table_id, restaurant)
    result = await db.db_update_table_position(
        table_id, body.position_x, body.position_y
    )
    if not result:
        return JSONResponse({"detail": "Table not found"}, status_code=404)
    return result


# ── Bulk floor plan save (admin-only) ─────────────────────────────────────
# Companion to the existing per-table PUT /position and PUT /properties: lets
# the floor plan editor persist many edits in one request when the user hits
# "Guardar layout". Each entry uses PATCH semantics — only fields present (and
# non-None) get persisted; the rest of the row is left untouched.

class FloorPlanTableUpdate(BaseModel):
    id: str
    position_x: float | None = Field(None, ge=-10000, le=10000)
    position_y: float | None = Field(None, ge=-10000, le=10000)
    capacity: int | None = Field(None, ge=1, le=100)
    table_type: str | None = None  # validated against _VALID_TABLE_TYPES below
    zone: str | None = Field(None, max_length=50)
    name: str | None = Field(None, max_length=60)


class FloorPlanBulkSaveBody(BaseModel):
    tables: list[FloorPlanTableUpdate] = Field(..., min_length=1, max_length=500)


@router.post("/api/tables/floor-plan")
async def save_floor_plan(
    body: FloorPlanBulkSaveBody,
    restaurant=Depends(get_current_restaurant_scoped),
    user=Depends(get_current_user),
):
    """Bulk save of floor plan layout. Admin / owner / gerente only.

    Each entry uses PATCH semantics: only non-None fields are persisted.
    Rows where the table belongs to another org are reported in `errors[]`
    rather than 403'ing the whole request — partial saves are useful when
    an editor session has stale ids.
    """
    role = (user.get("role") or "").lower()
    user_roles = {r.strip() for r in role.split(",") if r.strip()}
    if not (user_roles & {"owner", "admin", "gerente"}):
        raise HTTPException(
            status_code=403,
            detail="Solo owner, admin o gerente pueden editar el floor plan",
        )

    # table_type is validated against the same whitelist as the per-table
    # PUT /properties endpoint to avoid drift.
    for entry in body.tables:
        if entry.table_type is not None and entry.table_type not in _VALID_TABLE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"table_type must be one of: {', '.join(sorted(_VALID_TABLE_TYPES))} "
                    f"(table {entry.id})"
                ),
            )

    org_id = restaurant["id"]  # post-Wave-2: restaurant["id"] == org_id
    payload = [t.model_dump(exclude_none=True) for t in body.tables]

    result = await db.db_save_floor_plan_bulk(org_id=org_id, tables=payload)
    log.info(
        "floor_plan.saved",
        org_id=org_id,
        updated=result["updated"],
        skipped=result["skipped"],
        errors=len(result["errors"]),
    )
    return result


@router.put("/api/tables/{table_id}/properties")
async def update_table_properties(table_id: str, body: TablePropertiesBody, restaurant=Depends(get_current_restaurant_scoped)):
    """Updates a table's properties (capacity, table_type, zone)."""
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


def table_qr_url(request: Request, table_id: str) -> str:
    """Where a table's printed QR sends the diner.

    `/chat/{table_id}` — the diner's own web channel, which is the product:
    scanning opens the chat, the bot shows the carta as cards, and the order
    reaches the kitchen (closed product decision, docs/claude/status.md).

    Until 2026-09-24 every QR pointed at `/menu/{table_id}` instead, the
    catalog page whose printed sheet told the diner to "pedir por WhatsApp"
    — the channel being retired. A restaurant that printed its codes was
    handing customers the wrong flow on physical paper, which is the most
    expensive place to be wrong.
    """
    return f"{_public_base_url(request)}/chat/{table_id}"


def build_qr_html(menu_url: str, table_name: str, width: int = 300) -> str:
    return f"<!DOCTYPE html><html><head><meta charset='UTF-8'><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body style='margin:0;background:#fff;display:flex;align-items:center;justify-content:center;min-height:100vh;'><div id='qr'></div><script>window.onload=function(){{new QRCode(document.getElementById('qr'),{{text:decodeURIComponent('{urllib.parse.quote(menu_url)}'),width:{width},height:{width},colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});}};</script></body></html>"

def _public_base_url(request: Request) -> str:
    """Return the public base URL for QR generation.
    Uses APP_DOMAIN when set (Railway/production); falls back to request.base_url for local dev."""
    if _APP_DOMAIN:
        return f"https://{_APP_DOMAIN}"
    return str(request.base_url).rstrip('/')

_QR_SHEET_CSS = """
*{box-sizing:border-box;margin:0;padding:0;}
body{font-family:Arial,Helvetica,sans-serif;background:#f4f4f2;color:#0D1412;}
.bar{position:sticky;top:0;background:#fff;border-bottom:1px solid #e3e3e0;
     padding:14px 20px;display:flex;align-items:center;gap:14px;}
.bar h1{font-size:16px;font-weight:700;}
.bar .sub{font-size:13px;color:#666;}
.bar button{margin-left:auto;background:#1D9E75;color:#fff;border:0;border-radius:8px;
     padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer;}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;padding:20px;}
.card{background:#fff;border:2px solid #0D1412;border-radius:16px;padding:20px;
      text-align:center;break-inside:avoid;page-break-inside:avoid;}
.logo{font-size:22px;font-weight:900;}
.logo span{color:#1D9E75;}
.tname{font-size:19px;font-weight:700;margin:10px 0 2px;}
.instr{font-size:12px;color:#666;margin-bottom:12px;line-height:1.45;}
.qrbox{width:180px;height:180px;margin:0 auto 12px;}
.qrbox canvas,.qrbox img{width:180px !important;height:180px !important;}
.steps{text-align:left;background:#f8f8f5;border-radius:10px;padding:10px 14px;}
.step{font-size:11.5px;color:#444;padding:2px 0;display:flex;gap:7px;}
.sn{color:#1D9E75;font-weight:700;}
.empty{padding:60px 20px;text-align:center;color:#666;font-size:14px;}
@media print{
  body{background:#fff;}
  .bar{display:none;}
  .grid{padding:0;gap:0;grid-template-columns:repeat(2,1fr);}
  .card{border-radius:0;margin:0;}
}
"""


@router.get("/api/tables/qr-sheet", response_class=HTMLResponse)
async def get_all_tables_qr_sheet(request: Request):
    """Every table's QR on one printable page.

    A restaurant opening for the first time has to put a code on each
    table, and the only way to get them was one page per table — open the
    sheet, print, go back, next table. For twenty tables that is twenty
    round trips, on the very first day, which is exactly where onboarding
    is abandoned.

    Scoped through the same `_tables_scope` as the table listing, so a
    sheet can never contain a sede the user is not entitled to see.
    """
    await require_auth(request)
    org_id, branch_id = await _tables_scope(request)
    with tenant_scope(org_id):
        tables = await db.db_get_tables(branch_id=branch_id)

    cards = []
    for t in tables:
        table_id = str(t.get("id") or t.get("table_id") or "").strip()
        if not table_id:
            continue
        safe_name = _html.escape(str(t.get("name") or table_id))
        encoded = urllib.parse.quote(table_qr_url(request, table_id))
        cards.append(
            "<div class='card'>"
            "<div class='logo'>Mesio<span>.</span></div>"
            f"<div class='tname'>{safe_name}</div>"
            "<div class='instr'>Escanea el QR con la cámara<br>para ver la carta y pedir</div>"
            f"<div class='qrbox' data-qr='{encoded}'></div>"
            "<div class='steps'>"
            "<div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div>"
            "<div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div>"
            "<div class='step'><span class='sn'>3.</span><span>Elige tus platos de la carta</span></div>"
            "<div class='step'><span class='sn'>4.</span><span>Envía el pedido a la cocina</span></div>"
            "</div></div>"
        )

    body = (
        "<div class='grid'>" + "".join(cards) + "</div>"
        if cards else
        "<div class='empty'>Todavía no tienes mesas creadas.<br>"
        "Crea tus mesas y vuelve aquí para imprimir sus códigos.</div>"
    )
    count = len(cards)
    plural = "" if count == 1 else "s"

    # The QR images are drawn in the browser from data-qr, never built into
    # the HTML string: the URL is the only place a table name or id could
    # reach the page unescaped, and keeping it in an attribute the script
    # reads keeps that one value out of the markup it generates.
    return HTMLResponse(
        "<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Códigos QR de tus mesas — Mesio</title>"
        f"<style>{_QR_SHEET_CSS}</style>"
        "<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
        "</head><body>"
        "<div class='bar'><div><h1>Códigos QR de tus mesas</h1>"
        f"<div class='sub'>{count} mesa{plural} · imprime y pega uno en cada mesa</div></div>"
        "<button onclick='window.print()'>Imprimir</button></div>"
        f"{body}"
        "<script>window.onload=function(){"
        "document.querySelectorAll('.qrbox').forEach(function(el){"
        "new QRCode(el,{text:decodeURIComponent(el.dataset.qr),width:180,height:180,"
        "colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M});"
        "});};</script>"
        "</body></html>"
    )


@router.get("/api/tables/{table_id}/qr", response_class=HTMLResponse)
async def get_table_qr(request: Request, table_id: str):
    with bypass_tenant_scope("qr_public_lookup: pre-resolve table tenant for QR"):
        table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    return build_qr_html(table_qr_url(request, table_id), table["name"], width=300)

@router.get("/api/tables/{table_id}/qr-sheet")
async def get_qr_sheet(request: Request, table_id: str):
    with bypass_tenant_scope("qr_sheet_public_lookup: pre-resolve table tenant for QR sheet"):
        table = await db.db_get_table_by_id(table_id)
    if not table: raise HTTPException(status_code=404, detail="Mesa no encontrada")
    encoded = urllib.parse.quote(table_qr_url(request, table_id))
    safe_name = _html.escape(table['name'])
    return HTMLResponse(
        f"<!DOCTYPE html><html lang='es'><head><meta charset='UTF-8'><style>*{{box-sizing:border-box;margin:0;padding:0;}}body{{font-family:Arial,sans-serif;background:#fff;}}.page{{width:10cm;margin:1cm auto;text-align:center;padding:1.5cm;border:2px solid #0D1412;border-radius:16px;}}.logo{{font-size:28px;font-weight:900;color:#0D1412;margin-bottom:4px;}}.logo span{{color:#1D9E75;}}.tname{{font-size:20px;font-weight:700;color:#0D1412;margin:12px 0 4px;}}.instr{{font-size:13px;color:#666;margin-bottom:16px;line-height:1.5;}}.qrbox{{width:200px;height:200px;margin:0 auto 16px;}}.qrbox canvas,.qrbox img{{width:200px !important;height:200px !important;border-radius:8px;}}.wa-badge{{display:inline-flex;align-items:center;gap:6px;background:#25D366;color:white;padding:8px 16px;border-radius:100px;font-size:13px;font-weight:600;margin-bottom:16px;}}.steps{{text-align:left;background:#f8f8f5;border-radius:10px;padding:12px 16px;margin-top:8px;}}.step{{font-size:12px;color:#444;padding:3px 0;display:flex;gap:8px;}}.sn{{color:#1D9E75;font-weight:700;}}@media print{{body{{margin:0;}}}}</style><script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script></head><body><div class='page'><div class='logo'>Mesio<span>.</span></div><div class='tname'>{safe_name}</div><div class='instr'>Escanea el QR con la cámara<br>para ver la carta y pedir</div><div class='qrbox' id='qrc'></div><div class='wa-badge'>Ver la carta y pedir</div><div class='steps'><div class='step'><span class='sn'>1.</span><span>Abre la cámara de tu celular</span></div><div class='step'><span class='sn'>2.</span><span>Apunta al código QR</span></div><div class='step'><span class='sn'>3.</span><span>Elige tus platos de la carta</span></div><div class='step'><span class='sn'>4.</span><span>Envía el pedido a la cocina</span></div></div></div><script>window.onload=function(){{new QRCode(document.getElementById('qrc'),{{text:decodeURIComponent('{encoded}'),width:200,height:200,colorDark:'#0D1412',colorLight:'#ffffff',correctLevel:QRCode.CorrectLevel.M}});setTimeout(function(){{window.print();}},800);}};</script></body></html>"
    )

# ── ALERTAS MESERO ──────────────────────────────────────────────────
@router.get("/api/waiter-alerts")
async def get_waiter_alerts(request: Request):
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant.get("whatsapp_number", "")

    # A waiter sees THEIR OWN sede's alerts and nobody else's — the filter is
    # their staff row, not a header they control (memory: mesero-location-gap).
    # Owner/admin keep the org-wide view this screen has always had, and may
    # narrow it to one sede from the sidebar. The header value is verified to
    # belong to this org before it is used; resolve_sede_filter only decides
    # WHO may name a sede, not that the sede is theirs.
    user = await get_current_user(request)
    location_id = resolve_sede_filter(request, user)
    if location_id is not None and may_span_locations(user):
        try:
            with tenant_scope(restaurant["id"]):
                loc = await db.db_get_location_by_id(location_id)
        except Exception:
            loc = None
        if not loc or loc.get("org_id") != restaurant.get("id"):
            location_id = None

    try:
        with tenant_scope(restaurant["id"]):
            alerts = await tr.db_get_waiter_alerts(bot_number, location_id=location_id)
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
    """El administrador convoca a un mesero/empleado a caja o dashboard.

    SECURITY (2026-09 audit): `bot_number` used to come straight from the
    request BODY and the alert was written under bypass_tenant_scope — any
    authenticated user (of ANY restaurant) could push an alert onto another
    restaurant's waiter screen, and the row landed with org_id NULL (the
    bypass has no tenant to stamp). We now resolve the caller's OWN
    restaurant server-side and ignore whatever bot_number the body carries,
    writing inside tenant_scope() so RLS stamps the correct org_id and a
    cross-tenant bot_number in the body simply can't reach another org.
    """
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    bot_number = restaurant.get("whatsapp_number", "")
    with tenant_scope(restaurant["id"]):
        alert = await db.db_create_waiter_alert(
            phone=body.phone or "admin",
            bot_number=bot_number,
            alert_type="admin_call",
            message="El Administrador requiere verte en caja/dashboard",
            table_id=body.table_id,
            table_name=body.table_name,
            location_id=restaurant.get("location_id"),
        )
    return {"success": True, "alert": alert}

@router.post("/api/waiter-alerts/{alert_id}/dismiss")
async def dismiss_waiter_alert(request: Request, alert_id: int):
    """SECURITY (2026-09 audit): this used to run the UPDATE under
    bypass_tenant_scope with NO ownership check — alert ids are sequential
    integers, so any authenticated user of restaurant A could silence
    restaurant B's alerts by guessing ids (IDOR). Running it inside the
    caller's own tenant_scope() instead means RLS's org_isolation policy
    (waiter_alerts IS RLS-protected) makes the UPDATE match zero rows for a
    foreign or unknown id — we surface that as 404 and never touch the row.
    """
    await require_auth(request)
    restaurant = await get_current_restaurant(request)
    try:
        with tenant_scope(restaurant["id"]):
            dismissed = await tr.db_dismiss_waiter_alert(alert_id)
    except Exception:
        log.exception("tables.dismiss_waiter_alert_failed", alert_id=alert_id, restaurant_id=restaurant.get("id"))
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    if not dismissed:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
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


def _kitchen_delivery_location_filter(request: Request, user: dict) -> int | None:
    """Resolve which sede's delivery tickets a kitchen/caja/admin caller may
    see (docs/claude/delivery-web.md, "Known open items": this feed was
    org-scoped only, so every kitchen of a multi-sede org saw every sede's
    delivery tickets). Mirrors the X-Location-ID / own-staff.location_id
    convention app/routes/staff_delivery.py::delivery_scope already uses —
    same header name, same "admin picks explicitly, everyone else gets their
    own sede" shape — rather than inventing a second one.

    Unlike that stricter cashier-only surface, an admin caller here who sends
    NO header keeps this feed's EXISTING default (every sede) instead of
    being refused outright — this screen has always been usable org-wide by
    an admin, and chunk 8 only closes the "sees ANOTHER sede without asking"
    gap, not that existing convenience.

    Returns None (no filter -> every sede) only for an admin role with no
    header. Every other caller (kitchen/cocina/bar/caja/mesero/... or an
    admin WITH a header) gets a concrete int, or the call raises 403 when a
    non-admin has no sede of their own.
    """
    from app.services.staff_sections import ADMIN_ROLES, normalize_role  # noqa: PLC0415

    roles = {normalize_role(r) for r in (user.get("role") or "").split(",") if r.strip()}
    is_admin = bool(roles & ADMIN_ROLES)
    header = request.headers.get("X-Location-ID", "").strip()

    if is_admin:
        return int(header) if header.isdigit() else None

    raw_location_id = user.get("location_id")
    if not raw_location_id:
        raise HTTPException(status_code=403, detail="Tu usuario no tiene una sede asignada")
    return int(raw_location_id)


@router.get("/api/kitchen/delivery-orders")
async def get_delivery_orders(request: Request):
    user = await get_current_user(request)
    import json as _json

    # Tenant-scope the read so RLS filters to the authenticated admin's org.
    # Without this, db_get_delivery_orders_for_cashier returned ALL tenants' orders.
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    location_filter = _kitchen_delivery_location_filter(request, user)
    with tenant_scope(org_id):
        rows = await tr.db_get_delivery_orders_for_cashier(location_filter)
    orders = []
    for r in rows:
        items = r["items"]
        if isinstance(items, str):
            try: items = _json.loads(items)
            except: items = []
        orders.append({
            "id": r["id"],
            # The code the customer holds (/pedido/{code}) — what they say at pickup.
            "public_code": r.get("public_code"),
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


@router.patch("/api/kitchen/delivery-orders/{order_id}/status")
async def update_delivery_order_status(request: Request, order_id: str):
    user = await get_current_user(request)
    body = await request.json()
    new_status = body.get("status", "")
    valid = ["pendiente_pago", "confirmado", "en_preparacion", "listo", "en_camino", "entregado", "cancelado"]

    if new_status not in valid:
        raise HTTPException(status_code=400, detail="Estado inválido")

    # Tenant-scope the UPDATE so RLS rejects cross-tenant modifications.
    # Without this, any authenticated admin could PATCH any tenant's order.
    restaurant = await get_current_restaurant(request)
    org_id = restaurant["id"]
    location_filter = _kitchen_delivery_location_filter(request, user)

    # Web delivery/pickup orders (docs/claude/delivery-web.md) have their own
    # lifecycle: the cashier accepts, the kitchen only marks them ready, the
    # courier/cashier close them. The legacy path below would (a) let the
    # kitchen set ANY status, skipping acceptance, (b) never tell the
    # customer's status page, and (c) send WhatsApp messages and the WhatsApp
    # NPS to the order's `phone`, which for a web order is a `web:<uuid>`
    # identity — i.e. call Meta with an invalid number.
    with tenant_scope(org_id):
        routing = await delivery_repo.db_get_order_channel(org_id, order_id)
    if routing and routing.get("channel") == "web_chat":
        # Chunk 8: a kitchen must not act on another sede's order — same
        # "doesn't exist for you" 404 treatment delivery_repo.py's own
        # docstrings use for a caller who can't even see a row (as opposed
        # to a 409 for a real state conflict on a row they DO own).
        if location_filter is not None and int(routing.get("location_id") or -1) != location_filter:
            raise HTTPException(status_code=404, detail="Pedido no encontrado en tu sede.")
        if new_status != "listo":
            raise HTTPException(
                status_code=409,
                detail="Este pedido se gestiona desde Domicilios; la cocina solo lo marca listo.",
            )
        with tenant_scope(org_id):
            ready = await delivery_repo.db_mark_ready(org_id, order_id, location_filter)
        if not ready:
            raise HTTPException(
                status_code=409,
                detail="El pedido ya no está en preparación. Actualiza la pantalla.",
            )
        await realtime.publish_delivery_status(org_id, ready.get("location_id"), order_id)
        return {"success": True}

    with tenant_scope(org_id):
        await tr.db_update_delivery_order_status(order_id, new_status)

    # The customer follows the order on /pedido/{code} (live over SSE); the
    # WhatsApp status texts and the bot NPS that used to fire here are gone —
    # a web order's NPS is asked on that page (diner_delivery.py).
    if new_status == "confirmado":
        with bypass_tenant_scope("update_delivery_order_status: full order for billing"):
            order_row = await tr.db_get_delivery_order_full(order_id)
        if order_row:
            restaurant = await get_current_restaurant(request)
            config = await billing.get_billing_config(restaurant["id"])

            features = restaurant.get("features") or {}

            if not billing._is_dian_enabled(features):
                # DIAN gated off — skip auto-invoice silently (no folio purchased yet)
                log.info("dian.auto_invoice.skipped_disabled", restaurant_id=restaurant["id"], order_id=order_id)
            elif config:
                items = order_row["items"]
                if isinstance(items, str):
                    import json as _json
                    items = _json.loads(items)

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
                    log.exception(
                        "tables.billing_auto_invoice_failed",
                        order_id=order_id,
                        provider=provider,
                    )

    return {"success": True}

# ── TABLE ORDERS & OTHERS ──────────────────────────────────────────

@router.get("/api/table-orders")
async def get_table_orders(request: Request, status: str = None, station: str = None, table_id: str = None):
    """Returns table orders filtered by sede and status.

    Resolution rules:
      - owner/admin: every sede of the org, or ONE when they pick it from the
        sidebar (X-Branch-ID / X-Location-ID = digit).
      - everyone else (mesero, caja, cocina, bar, gerente): their OWN sede,
        from their staff row. Never a header.

    That second rule is the fix (PM 2026-09-20). The docstring already
    claimed "location_id = user.branch_id when staff is pinned to a sede",
    but the code never read it: `location_id` was set ONLY from the header
    and ONLY for admins, so every non-admin fell through to `None` and the
    kitchen of one sede got the comandas of every sede in the org.
    """
    user = await get_current_user(request)
    # Substring matching on the joined role string used to decide this
    # ("admin" also matches inside other words); roles_of() splits properly.
    is_admin = may_span_locations(user)

    # Resolve org_id (canonical tenant key post-Wave-2). For owner/admin/gerente
    # we rely on the restaurant lookup; for staff (mesero/caja/...) the
    # user.branch_id is also the org_id today — same value path either way.
    try:
        restaurant = await get_current_restaurant(request)
        org_id = restaurant.get("org_id") or restaurant.get("id")
    except HTTPException:
        org_id = user.get("restaurant_id") or user.get("branch_id")

    # Without an org_id the repo's `is_admin + no filters` branch returns
    # EVERY tenant's table orders (it runs under bypass_tenant_scope below).
    # Refuse instead of leaking across tenants.
    if not org_id:
        raise HTTPException(status_code=403, detail="No se pudo resolver tu organización")

    # Specific sede: the sidebar dropdown's location_id for an admin, the
    # caller's own staff row for everyone else.
    location_id = resolve_sede_filter(request, user)

    with bypass_tenant_scope("get_table_orders: may span branches or be admin view"):
        rows = await tr.db_get_table_orders_for_branch(
            branch_id=location_id,
            status=status,
            is_admin=is_admin,
            org_id=org_id,
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

    if table_id:
        result = [r for r in result if str(r.get("table_id", "")) == str(table_id)]

    return {"orders": result}

@router.get("/api/table-orders/{order_id}/ticket")
async def get_order_ticket(request: Request, order_id: str):
    """
    Returns the structured data for a ticket, aggregating all
    sub-orders with the same base_order_id.
    Includes fiscal data (CUFE, QR) if an invoice has been issued.
    """
    import json as _json
    user = await get_current_user(request)
    branch_id = user.get("branch_id")

    with bypass_tenant_scope("get_order_ticket: ticket lookup by order_id across branches"):
        rows = await tr.db_get_table_orders_by_base_id(order_id, branch_id)

    if not rows:
        raise HTTPException(status_code=404, detail="Orden no encontrada")

    # Aggregate items and totals from all sub-orders
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

    # Fiscal data: last invoice issued for this order (deferred to billing layer).
    # Use tenant_connection so the lookup inherits the active bypass_tenant_scope
    # set above (admins legitimately view tickets across branches). Raw pool.acquire
    # would create a new connection without the GUC — RLS-blocked under mesio_app
    # in prod, accidentally cross-tenant under postgres in test.
    fiscal = None
    try:
        from app.services.tenant_db import tenant_connection as _tc  # noqa: PLC0415
        async with _tc() as conn:
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
        # Don't crash the ticket endpoint if billing config is missing or DIAN
        # tables aren't provisioned. But DO log — silent except hid real RLS
        # errors during the audit.
        log.exception("tables.ticket.fiscal_lookup_failed", order_id=order_id)

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

    session_data = None
    if phone and phone != "manual":
        try:
            with bypass_tenant_scope("update_order_status: session lookup by phone"):
                session = await tr.db_get_open_table_session_by_phone(phone)
            if session:
                session_data = session
        except Exception:
            log.exception("tables.session_lookup_error", phone=phone)

    if status == "generar_factura":
        base_id = order.get("base_order_id") or order_id
        with bypass_tenant_scope("update_order_status: mark factura generada by order ID"):
            await db.db_mark_invoice_generated(base_id)
        return {"success": True, "order_id": order_id, "status": "factura_generada"}

    if status in ("cerrar_mesa", "factura_entregada"):
        base_id = order.get("base_order_id") or order_id
        with bypass_tenant_scope("update_order_status: close table bill by order ID"):
            await db.db_close_table_bill(base_id)
        if phone and phone != "manual":
            await _farewell_and_nps(phone, order.get("table_id"), session_data, username)
        return {"success": True, "order_id": order_id, "status": "factura_entregada"}

    # ── C. ESTADOS NORMALES (Prep, Listo, Entregado) ──
    else:
        with bypass_tenant_scope("update_order_status: normal status update by order ID"):
            await db.db_update_table_order_status(order_id, status)
        # The diner sees "listo"/"entregado" in their chat (SSE table_order_updated).
        _bot_number = (session_data.get("bot_number") if session_data else None) or order.get("bot_number", "")
        if status == "listo" and phone and phone != "manual":
            # Notify the assigned mesero that food is ready at the pass.
            # Best-effort: failure to create the alert MUST NOT block the
            # customer notification or the status update. Same pattern as
            # the bill_request alert documented in CLAUDE.md Regla #17.
            #
            # We use tenant_scope (NOT bypass) here because db_create_waiter_alert
            # reads app.org_id via current_setting() to populate the NOT NULL
            # org_id column. The order row was loaded above and has org_id, so
            # we have the right tenant identity.
            _order_org_id = order.get("org_id")
            if _order_org_id is not None:
                try:
                    with tenant_scope(int(_order_org_id)):
                        await db.db_create_waiter_alert(
                            phone=phone,
                            bot_number=_bot_number,
                            alert_type="ready",
                            message=f"Pedido listo en pase — Mesa {table_name}",
                            table_id=order.get("table_id", ""),
                            table_name=table_name,
                            location_id=order.get("location_id"),
                        )
                except Exception:
                    log.exception(
                        "tables.waiter_alert_listo_failed",
                        order_id=order_id,
                        table_id=order.get("table_id"),
                        org_id=_order_org_id,
                    )

    return {"success": True, "order_id": order_id, "status": status}

# ── MÓDULO PUNTO DE VENTA (POS) PARA MESEROS ─────────────────────────

class ManualOrderRequest(BaseModel):
    table_id:   str
    table_name: str
    items:      list
    total:      Decimal
    notes:      str = ""
    station:    str = "all"
    branch_id:  int = None  # 🛡️ Added branch_id to the model
    
@router.get("/api/pos/menu")
async def get_pos_menu(request: Request):
    """Returns the restaurant's menu for rendering in the waiter's POS.

    Wave-2: the menu lives at the org level (organizations.menu). The wa_number
    used for the menu lookup must come from the staff's actual sede (resolved
    via user.branch_id → location.whatsapp_number); we no longer fall back
    to "any restaurant globally" — that would render another customer's menu
    in this customer's POS (cross-tenant leak).
    """
    user = await get_current_user(request)

    if not user or not user.get("org_id"):
        # Cannot resolve the caller's org → an empty menu rather than a
        # cross-tenant one. The frontend shows "menu not configured".
        return {"menu": {}}

    # The POS sells at ONE sede, at that sede's prices (migration 0093).
    # owner/admin may pick the sede they are working; everyone else is
    # pinned to their own.
    org_id = int(user["org_id"])
    sede = resolve_sede_filter(request, user, admin_without_header="own")
    with tenant_scope(org_id):
        menu = await sede_menu.get_sede_menu(org_id, sede if isinstance(sede, int) else None)
    return {"menu": menu, "location_id": sede}

@router.get("/api/pos/tables-status")
async def get_tables_status(request: Request):
    """Returns all tables and their current status (ideal for rendering the map)"""
    await require_auth(request)

    # 1. Smart context resolution
    restaurant = await get_current_restaurant(request)

    # Wave-2 model: every restaurant is a `locations` row. There is NO special
    # "matriz" entity at the data layer — `is_primary=true` simply marks the
    # primary sede of an org. We need TWO distinct integers here:
    #   - org_id        : tenant key for tenant_scope() / RLS GUC
    #   - location_id   : the sede id stored in restaurant_tables.branch_id
    # Both are consistently populated by db_get_restaurant_by_org_id / by_location_id
    # (and by db_get_all_restaurants post the same-paso fix). If location_id is
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
        enrichment = await tr.db_get_tables_status_enrichment(location_id)

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
        # Enrichment fields for the mesero / POS frontend
        enc = enrichment.get(tid, {})
        t['has_waiter_alert']   = enc.get('has_waiter_alert',   False)
        t['has_open_check']     = enc.get('has_open_check',     False)
        t['current_total']      = enc.get('current_total',      0.0)
        t['session_active']     = enc.get('session_active',     False)
        t['session_started_at'] = enc.get('session_started_at', None)
        t['active_order_id']    = enc.get('active_order_id',    None)
        t['channel']            = enc.get('channel',            None)
        t['waiter_staff_id']    = enc.get('waiter_staff_id',    None)
        t['assigned_staff_id']  = enc.get('assigned_staff_id',  None)
        t['waiter_name']        = enc.get('waiter_name',        None)

    return {"tables": tables}

# ── Capa 3: Anti-impostor validation endpoints ────────────────────────────────

@router.post("/api/waiter/tables/{table_id}/confirm-real")
async def confirm_table_real(request: Request, table_id: str):
    """Waiter confirms the customer is real at this table.

    Releases all pending_table_validation orders to the kitchen queue and
    marks the session as verified (subsequent orders go straight to kitchen).

    Auth: any authenticated staff role (mesero, caja, admin, owner, gerente).
    """
    # require_auth() returns the username string; get_current_user() returns the dict.
    # Original Capa 3 endpoint mistakenly called user.get(...) on the str. Production
    # bug — would crash on every real call. Caught by E2E test.
    username = await require_auth(request)
    user = await get_current_user(request)
    role = user.get("role", "")
    allowed_roles = {"owner", "admin", "gerente", "mesero", "caja"}
    if not any(r in role for r in allowed_roles):
        raise HTTPException(status_code=403, detail="Rol no autorizado")

    try:
        restaurant = await get_current_restaurant(request)
        org_id = restaurant.get("org_id") or restaurant.get("id")
    except HTTPException:
        org_id = user.get("restaurant_id") or user.get("branch_id")

    with bypass_tenant_scope("confirm_table_real: release pending validation orders"):
        released = await tr.db_confirm_table_real(table_id, org_id, username)

    log.info(
        "mesero.confirm_table_real",
        table_id=table_id,
        org_id=org_id,
        confirmed_by=username,
        orders_released=released,
    )
    return {
        "success": True,
        "table_id": table_id,
        "orders_released": released,
        "message": f"{released} pedido(s) liberado(s) a cocina.",
    }


@router.post("/api/waiter/tables/{table_id}/mark-ghost")
async def mark_table_ghost(request: Request, table_id: str):
    """Waiter marks this table as a ghost (no real customer present).

    Cancels all pending_table_validation orders, closes active sessions
    with status='ghost_blocked', and adds the phone numbers to the
    phone_blocklist for 24 hours.

    Auth: any authenticated staff role (mesero, caja, admin, owner, gerente).
    """
    # Same fix as confirm-real above: require_auth() returns username str,
    # get_current_user() returns the user dict.
    username = await require_auth(request)
    user = await get_current_user(request)
    role = user.get("role", "")
    allowed_roles = {"owner", "admin", "gerente", "mesero", "caja"}
    if not any(r in role for r in allowed_roles):
        raise HTTPException(status_code=403, detail="Rol no autorizado")

    try:
        restaurant = await get_current_restaurant(request)
        org_id = restaurant.get("org_id") or restaurant.get("id")
    except HTTPException:
        org_id = user.get("restaurant_id") or user.get("branch_id")

    with bypass_tenant_scope("mark_table_ghost: cancel orders and close sessions"):
        result = await tr.db_mark_table_ghost(table_id, org_id, username)

    phones = result.get("phones", [])

    # Block each phone for 24 hours.
    if phones:
        from app.repositories.phone_blocklist_repo import add_to_blocklist
        for phone in phones:
            try:
                with bypass_tenant_scope("mark_table_ghost: add phone to blocklist"):
                    await add_to_blocklist(
                        phone=phone,
                        org_id=org_id,
                        reason="ghost_table_marked",
                        blocked_by=username,
                        hours=24,
                    )
            except Exception:
                log.exception(
                    "mesero.ghost_blocklist_add_failed",
                    phone=phone,
                    table_id=table_id,
                )

    log.info(
        "mesero.mark_table_ghost",
        table_id=table_id,
        org_id=org_id,
        marked_by=username,
        orders_cancelled=result.get("orders_cancelled", 0),
        phones_blocked=len(phones),
    )
    return {
        "success": True,
        "table_id": table_id,
        "orders_cancelled": result.get("orders_cancelled", 0),
        "phones_blocked": len(phones),
        "message": f"Mesa marcada como fantasma. {result.get('orders_cancelled', 0)} pedido(s) cancelado(s), {len(phones)} teléfono(s) bloqueado(s).",
    }


@router.patch("/api/table-orders/{base_order_id}/adjust")
async def adjust_table_bill(request: Request, base_order_id: str):
    """Adjusts an invoice's items and total before charging (discounts, tip, etc.)"""
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
    
    # 🛡️ BRANCH RESOLUTION
    # If it comes in the body we use it, otherwise use the user's (waiter/admin)
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
    tax_pct: float = 19.0        # sent by the client from the billing config
    tax_regime: str = "iva"

class PaymentMethod(BaseModel):
    method: str    # efectivo | tarjeta | nequi | transferencia
    amount: float

class PayCheckBody(BaseModel):
    payments: list[PaymentMethod] = []
    customer_name: str = Field("Consumidor Final", max_length=200)
    customer_nit: str = Field("222222222", max_length=30, pattern=r"^[\d\-]{6,30}$")
    customer_email: str = Field("", max_length=254)
    service_charge: float = 0.0  # Service charge as an absolute value (e.g. 10% of the subtotal)
    tip_amount: float = Field(0.0, ge=0.0)

    @field_validator("customer_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re as _re
        if v and not _re.match(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{1,63}$", v):
            raise ValueError("Dirección de email inválida")
        return v


@router.post("/api/table-orders/{base_order_id}/checks")
async def create_checks(request: Request, base_order_id: str, body: CreateChecksBody):
    """
    Creates or replaces a table's bill split.
    Validates quantity integrity against the original ticket.
    Calculates subtotal/tax/total server-side (does not trust the client).
    """
    user = await get_current_user(request)

    # Get the full ticket to validate quantities
    # First try with the user's branch filter; if nothing found (e.g. Matriz admin
    # handling a branch order), retry without the branch filter. The ownership
    # check below still enforces restaurant boundaries.
    with bypass_tenant_scope("create_checks: ticket lookup by order ID across branches"):
        ticket = await db.db_get_order_ticket_data(base_order_id, user.get("location_id") or None)
        if not ticket:
            ticket = await db.db_get_order_ticket_data(base_order_id, None)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")

    # Map of available qty per dish in the original ticket
    available: dict[str, int] = {}
    for item in ticket.get("items", []):
        key = item["name"].strip().lower()
        available[key] = available.get(key, 0) + int(item.get("quantity", item.get("qty", 1)))

    # Ownership check: ticket must belong to this user's org (Wave-2 tenant boundary)
    # P0 fix (2026-09): use the explicit org_id off the user dict directly —
    # no DB round-trip, and no risk of the old branch_id guess resolving to
    # an unrelated org (this was the pay_check tenant-scope P0: user["branch_id"]
    # is mixed-kind and was being fed into the now-deleted ambiguous lookup).
    ticket_org_id = ticket.get("org_id")
    user_org_id = user.get("org_id") or user.get("restaurant_id")
    # Fail closed: if either side is unresolvable, deny rather than allow cross-tenant write
    if ticket_org_id is None or user_org_id is None or int(ticket_org_id) != int(user_org_id):
        raise HTTPException(status_code=403, detail="Este ticket no pertenece a tu organización")

    # Validate that the checks don't exceed the available quantities
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

    # Validate that the breakdown covers ALL the ticket's items (not just that it doesn't exceed)
    for name, avail_qty in available.items():
        assigned = check_totals.get(name, 0)
        if assigned < avail_qty:
            raise HTTPException(
                status_code=400,
                detail=f"El desglose no cubre todos los ítems. Faltan: {name} x{avail_qty - assigned}"
            )

    # Build checks with server-side calculated totals
    tax_factor = to_decimal(body.tax_pct) / Decimal("100")
    validated = []
    for chk in body.checks:
        # Rebuild items with unit_price from the ticket (looked up by name)
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
    """Lists all of a table's checks with their fiscal data."""
    await get_current_user(request)
    with bypass_tenant_scope("get_checks: checks lookup by order ID across branches"):
        checks = await db.db_get_checks(base_order_id)
    return {"checks": checks}

@router.post("/api/table-orders/{base_order_id}/checks/single/pay")
async def pay_check_single(request: Request, base_order_id: str, body: PayCheckBody):
    """
    Charges the whole table in a single check (no prior split).

    Atomically creates a single check with ALL the ticket's items and charges it
    reusing the pay_check flow (fiscal, loyalty, NPS, change, tip).

    The cashier calls this when the user selects "Pagar mesa completa" without
    having split the bill. The real check_id is returned in the response so
    the frontend can reference it later if needed.
    """
    user = await get_current_user(request)

    # TOCTOU guard — serialize concurrent single-pay attempts per table.
    rl_key = f"pay_single:{base_order_id}"
    if not await state_store.rate_limit_check(rl_key, max_requests=1, window_seconds=15):
        raise HTTPException(status_code=429, detail="Ya hay un cobro de mesa en proceso. Espera unos segundos.")

    # Fetch ticket — cross-branch bypass mirrors the pattern in create_checks.
    # db_get_order_ticket_data's branch_id param is a LOCATION id
    # (table_orders.branch_id == location_id) — user["location_id"] is the
    # correct explicit field for it (was user["branch_id"], mixed-kind).
    with bypass_tenant_scope("pay_check_single: ticket lookup across branches"):
        ticket = await db.db_get_order_ticket_data(base_order_id, user.get("location_id") or None)
        if not ticket:
            ticket = await db.db_get_order_ticket_data(base_order_id, None)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")

    # Ownership check: ticket must belong to this user's org.
    # P0 fix (2026-09): use the explicit org_id off the user dict directly —
    # no DB round-trip, and no risk of the old branch_id guess resolving to
    # an unrelated org (this was the pay_check tenant-scope P0).
    ticket_org_id = ticket.get("org_id")
    user_org_id = user.get("org_id") or user.get("restaurant_id")
    if ticket_org_id is None or user_org_id is None or int(ticket_org_id) != int(user_org_id):
        raise HTTPException(status_code=403, detail="Este ticket no pertenece a tu organización")

    # Refuse if the order already has any non-cancelled check — caller should use /checks/{id}/pay
    with bypass_tenant_scope("pay_check_single: existing checks guard"):
        existing = await db.db_get_checks(base_order_id)
    if any(c.get("status") != "cancelled" for c in (existing or [])):
        raise HTTPException(
            status_code=409,
            detail="Esta mesa ya tiene checks abiertos o cobrados. Usá el endpoint de check específico.",
        )

    # Build a SINGLE check with ALL items, server-computed totals.
    items_out = []
    gross = Decimal("0")
    for item in ticket.get("items", []):
        name = item["name"]
        qty = int(item.get("quantity", item.get("qty", 1)))
        unit_price = to_decimal(item.get("price", 0))
        items_out.append({
            "name":       name,
            "qty":        qty,
            "unit_price": float(unit_price),                          # JSON boundary
            "subtotal":   float(money_mul(unit_price, qty)),          # JSON boundary
        })
        gross += money_mul(unit_price, qty)

    # For a single full-table check we treat the ticket's total as gross.
    # Tax factor is 0 here — split checks can pass tax_pct on creation, but
    # the single pay path uses whatever tax was already computed into the ticket.
    total      = quantize_money(gross)
    subtotal   = total
    tax_amount = Decimal("0")

    single_check_payload = [{
        "check_number": 1,
        "items":        items_out,
        "subtotal":     float(subtotal),
        "tax_amount":   float(tax_amount),
        "total":        float(total),
    }]

    with bypass_tenant_scope("pay_check_single: create single check"):
        created = await db.db_create_checks(base_order_id, single_check_payload)
    if not created:
        raise HTTPException(status_code=500, detail="No se pudo crear el check")

    created_check = created[0] if isinstance(created, list) else created
    check_id = str(created_check.get("id") or created_check.get("check_id"))
    if not check_id:
        raise HTTPException(status_code=500, detail="Check creado sin id — estado inconsistente")

    # Delegate to the existing pay_check — it handles rate-limit, tenant_scope,
    # fiscal invoice (gated by dian_active flag; currently OFF), loyalty accrual,
    # NPS farewell, and change calculation in one coherent path.
    return await pay_check(request, base_order_id, check_id, body)


@router.post("/api/table-orders/{base_order_id}/checks/{check_id}/pay")
async def pay_check(request: Request, base_order_id: str, check_id: str, body: PayCheckBody):
    # _claimed tracks whether we hold the check in 'paying' state. If we exit
    # via an error after claiming but before db_finalize_check_payment commits,
    # we must release the claim so a retry can succeed.
    _claimed = False
    try:
        # Rate limit: 3 payments per check per 10 seconds (prevents double-click)
        from app.services import state_store
        rl_key = f"pay:{check_id}"
        if not await state_store.rate_limit_check(rl_key, max_requests=3, window_seconds=10):
            raise HTTPException(status_code=429, detail="Demasiadas solicitudes de pago. Intenta de nuevo en unos segundos.")
        restaurant = await get_current_restaurant(request)

        # Ambient scope for the ENTIRE payment flow — this used to be 8 separate
        # `with tenant_scope(...)` blocks sprinkled through the function, and the
        # billing.get_billing_config() call below was accidentally left outside
        # ALL of them, so every single table payment raised TenantNotSetError.
        # A route with N manual scope blocks WILL eventually miss one — pin the
        # scope once, for the whole handler, so a missed call site is structurally
        # impossible. NOTE: `_farewell_and_nps(...)` below is deliberately called
        # AFTER this block exits — it internally uses bypass_tenant_scope() for
        # cross-tenant phone lookups (NPS/session cleanup are keyed by phone, not
        # restaurant), and bypass_tenant_scope() raises TenantContextConflict if a
        # tenant scope is already pinned. Do NOT move that call inside this block.
        with tenant_scope(restaurant["id"]):
            # Atomic claim: SELECT FOR UPDATE + transition open→paying. Two cashiers
            # paying concurrently — only the first wins; the second gets None and
            # the request fails with 409 BEFORE any DIAN invoice is generated.
            check = await db.db_claim_check_for_payment(check_id, base_order_id)

            if check is None:
                # Could be: not found, wrong order, or already paying/invoiced/cancelled.
                # We do a follow-up read to give a precise error message.
                existing = await db.db_get_check(check_id)
                if not existing:
                    raise HTTPException(status_code=404, detail="Check no encontrado")
                if existing["base_order_id"] != base_order_id:
                    raise HTTPException(status_code=400, detail="El check no pertenece a este ticket")
                raise HTTPException(status_code=409, detail=f"Este check ya fue procesado (status: {existing['status']})")
            _claimed = True

            # If no payments were sent, use the check's proposed_payments (bot flow)
            if not body.payments:
                proposed = check.get("proposed_payments")
                if isinstance(proposed, str):
                    import json as _json
                    proposed = _json.loads(proposed)
                if proposed:
                    body.payments = [PaymentMethod(method=p["method"], amount=p["amount"]) for p in proposed]
                else:
                    raise HTTPException(status_code=400, detail="No se especificaron métodos de pago")

            # Also use the proposed tip if no explicit tip was sent and one is stored
            if body.tip_amount == 0.0 and check.get("proposed_tip"):
                body.tip_amount = float(to_decimal(check["proposed_tip"]))

            total_paid = to_decimal(sum(p.amount for p in body.payments))
            check_total  = to_decimal(check["total"]) + to_decimal(body.service_charge)
            if total_paid < check_total:
                raise HTTPException(status_code=400, detail=f"Pago insuficiente: se requieren ${float(check_total):,.0f}, se recibieron ${float(total_paid):,.0f}")

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

            change = float(quantize_money(total_paid - check_total, _currency))

            tip_amount_d = to_decimal(body.tip_amount)
            tip_cap_base = to_decimal(check["total"]) + to_decimal(body.service_charge)
            if tip_amount_d > 0 and tip_amount_d > money_mul(tip_cap_base, Decimal("0.5")):
                raise HTTPException(status_code=400, detail="La propina no puede superar el 50% del total")

            config = await billing.get_billing_config(restaurant["id"])

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
            if config and billing._is_dian_enabled(features):
                config["_restaurant_id"] = restaurant["id"]
                provider = config.get("provider", "mesio_native")
                adapter  = billing.get_adapter(provider)
                try:
                    fiscal = await adapter.create_invoice(order_for_billing, config)
                except Exception as exc:
                    # DIAN failed AFTER we claimed the check. Release the claim so
                    # the cashier can retry without waiting for the lock to expire.
                    # The except below would also do this via the _claimed flag, but
                    # being explicit here keeps the rollback close to the failure.
                    raise HTTPException(status_code=500, detail=f"Error al emitir factura: {exc}")
                fiscal_invoice_id = fiscal["id"]
            else:
                fiscal = {"id": None, "local": True}

            payments_list = [{"method": p.method, "amount": p.amount} for p in body.payments]

            finalized = await db.db_finalize_check_payment(
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
            if not finalized:
                # The claim was lost between db_claim_check_for_payment and here
                # (extremely unlikely — would require external state mutation).
                # Treat as 409 and DO NOT proceed to loyalty accrual / NPS.
                _claimed = False  # don't release a claim that's no longer ours
                log.warning("tables.pay_check.finalize_no_op", check_id=check_id, base_order_id=base_order_id)
                raise HTTPException(status_code=409, detail="El check fue modificado por otra operación. Refresca la pantalla.")
            # From here on, the check is invoiced. No release on subsequent errors.
            _claimed = False

            if hasattr(loyalty_svc, "accrue_on_check"):
                _loyalty_org_id = restaurant["id"]
                _loyalty_bot    = restaurant.get("whatsapp_number", "")
                _loyalty_boid   = base_order_id
                _loyalty_cid    = check_id
                _loyalty_total  = float(to_decimal(check["total"]) + to_decimal(body.service_charge))

                async def _accrue_with_scope(
                    rid=_loyalty_org_id, bn=_loyalty_bot,
                    boid=_loyalty_boid, cid=_loyalty_cid, total=_loyalty_total,
                ):
                    # Explicit re-pin: this runs as a detached asyncio.Task which may
                    # outlive this request's ambient scope above (context is copied
                    # at task-creation time, but being explicit here is defensive and
                    # keeps this coroutine correct if ever awaited directly instead).
                    with tenant_scope(rid):
                        await loyalty_svc.accrue_on_check(
                            restaurant_id=rid,
                            bot_number=bn,
                            base_order_id=boid,
                            check_id=cid,
                            total_cop=total,
                        )

                asyncio.create_task(_accrue_with_scope())
            else:
                log.warning("tables.loyalty_accrue_not_implemented", check_id=check_id)

            order_row = await db.db_get_first_table_order(base_order_id)
            farewell_targets = []
            if order_row and order_row["status"] == "factura_entregada":
                # Whole table just settled (every check invoiced/cancelled).
                # Notify EVERY distinct diner who ordered here — not just
                # order_row's phone (which is only the FIRST table_orders
                # row's phone, i.e. whoever opened the table). On a normal
                # single-phone WhatsApp table every row shares that same
                # phone, so `distinct_phones` collapses to exactly one value
                # and behaviour is unchanged; on a shared diner-web table
                # (app/routes/diner.py — each participant's own "web:<uuid4>"
                # phone) every diner who actually had orders here gets their
                # own farewell/NPS trigger.
                distinct_phones: list[str] = []
                seen_phones: set[str] = set()
                try:
                    table_rows = await tr.db_get_table_orders_by_base_id(base_order_id)
                except Exception:
                    log.exception("tables.pay_check.farewell_targets_lookup_failed", base_order_id=base_order_id)
                    table_rows = []
                for row in table_rows:
                    p = row.get("phone")
                    if not p or p == "manual" or p in seen_phones:
                        continue
                    if row.get("status") in ("cancelado", "cancelled"):
                        continue
                    seen_phones.add(p)
                    distinct_phones.append(p)
                if not distinct_phones and order_row.get("phone") and order_row["phone"] != "manual":
                    distinct_phones = [order_row["phone"]]

                for customer_phone in distinct_phones:
                    sess = await db.db_get_open_session_by_phone(customer_phone)
                    farewell_targets.append((customer_phone, order_row.get("table_id"), sess))

        # Outside the ambient scope on purpose — see comment above the `with` block.
        for args in farewell_targets:
            await _farewell_and_nps(*args, "caja")

        return {
            "success":  True,
            "check_id": check_id,
            "change":   change,
            "fiscal":   fiscal,
        }
    except HTTPException:
        # Release the claim so the cashier can retry without waiting.
        # Only releases if we still hold it (status='paying').
        if _claimed:
            try:
                with tenant_scope(restaurant["id"]):
                    await db.db_release_check(check_id)
            except Exception:
                log.exception("tables.pay_check.release_failed", check_id=check_id)
        raise
    except Exception as e:
        if _claimed:
            try:
                with tenant_scope(restaurant["id"]):
                    await db.db_release_check(check_id)
            except Exception:
                log.exception("tables.pay_check.release_failed", check_id=check_id)
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
    """Attaches proof of payment to checks with an awaiting_proof proposal."""
    await get_current_user(request)
    with bypass_tenant_scope("attach_proof: proof attachment by order ID across branches"):
        updated = await db.db_attach_proof(base_order_id, body.customer_phone, body.media_url)
    if not updated:
        raise HTTPException(status_code=404, detail="No hay propuesta awaiting_proof para este teléfono")
    return {"success": True}


@router.get("/api/checkout-proposals")
async def list_checkout_proposals(request: Request):
    """
    Lists tables with active bot payment proposals (pending/awaiting_proof/proof_received).
    For the 'Por Confirmar' tab in cashier.html.
    """
    restaurant = await get_current_restaurant(request)
    # Cashier-facing: a cajero sees the proposals of their own sede. Only
    # owner/admin may look at another one, or at all of them at once.
    user = await get_current_user(request)
    location_id = resolve_sede_filter(request, user)
    branch_ids = [location_id] if location_id is not None else None

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
    """Returns the check data for thermal receipt printing."""
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
    customer_name: str = Field("Consumidor Final", max_length=200)
    customer_nit: str = Field("222222222", max_length=30, pattern=r"^[\d\-]{6,30}$")
    customer_email: str = Field("", max_length=254)
    order_type: str = "salon"     # salon | domicilio
    table_name: str = "Caja"
    branch_id: int | None = None

    @field_validator("customer_email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re as _re
        if v and not _re.match(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{1,63}$", v):
            raise ValueError("Dirección de email inválida")
        return v


@router.post("/api/pos/quick-invoice")
async def pos_quick_invoice(request: Request, body: QuickInvoiceBody):
    """
    Creates a quick sale from the register without going through the table/bot flow.
    Creates an ephemeral table_order, a check, and pays it in a single step.
    """
    restaurant = await get_current_restaurant(request)
    user = await get_current_user(request)

    # P1 fix (2026-09-17): `user.get("branch_id")` is the AMBIGUOUS legacy
    # column (users.branch_id — some writers stored org_id, others
    # location_id; see docs/claude/rls-multitenant.md). Falling back further
    # to `restaurant["id"]` (the ORG id) was even worse: it substituted an
    # org_id for a location_id, exactly the conflation CLAUDE.md forbids.
    # `user.get("location_id")` is the explicit, unambiguous field (backfilled
    # by migration 0081). If neither the caller nor the user carries a real
    # location, branch_id stays NULL — table_orders.branch_id is nullable —
    # rather than guessing.
    branch_id = body.branch_id or user.get("location_id")

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
        # P1 fix (2026-09-17): table_orders.table_id is TEXT NOT NULL with no
        # default and no FK to restaurant_tables — quick-invoice has no real
        # table, so `None` here always violated the NOT NULL constraint (this
        # was masked in practice because db_save_table_order's own org_id
        # resolution — see below — raised ValueError first). Reuse the
        # synthetic order_id: it's unique (uuid4-based) and never collides
        # with a real table id (those look like "table-{org_id}-{number}",
        # see db_create_table), so it can't be mistaken for one anywhere that
        # joins on table_id.
        "table_id": order_id,
        # db_save_table_order cannot resolve a tenant from table_id alone for
        # a synthetic id like the one above — it used to raise ValueError
        # trying. This route already knows the ORG id (it's what it scopes
        # tenant_scope() with below) — pass it through explicitly instead of
        # relying on the table_id lookup.
        "org_id": restaurant["id"],
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
    # Ambient scope for the ENTIRE quick-invoice flow — same fix as pay_check()
    # above. This handler used to have the create/finalize DB calls each in
    # their own `with tenant_scope(...)` block with the DIAN billing calls
    # (get_billing_config / adapter.create_invoice) left unscoped in between.
    # Both are tenant_connection()-backed repo calls (fiscal_repo), so any
    # restaurant with DIAN enabled would 500 on quick-invoice exactly like
    # pay_check did. Pinning the scope once for the whole block makes that
    # class of bug structurally impossible here too.
    #
    # _claimed tracks whether we hold the check in 'paying' state (mirrors
    # pay_check()'s pattern below) — release it on any failure after the
    # claim so a retry isn't stuck behind a check nothing will ever finalize.
    check_id = None
    _claimed = False
    with tenant_scope(restaurant["id"]):
        try:
            await db.db_save_table_order(order)

            # Create a single check for this sale
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

            # P1 fix (2026-09-17): db_create_checks always creates in status
            # 'open'. db_finalize_check_payment only commits a check that is
            # in status 'paying' (its documented pre-condition — see
            # db_claim_check_for_payment's docstring) and silently returns
            # False otherwise. Calling finalize directly on an 'open' check
            # (as this route used to) is a same-shape bug to the org_id one:
            # it looked like a working call but was structurally a no-op —
            # the route reported success while the check stayed unpaid.
            claimed_check = await db.db_claim_check_for_payment(check_id, base_order_id)
            if claimed_check is None:
                raise HTTPException(status_code=500, detail="No se pudo reservar el check para el pago")
            _claimed = True

            # Billing / DIAN (opcional)
            features = restaurant.get("features") or {}
            if isinstance(features, str):
                import json as _json
                try:
                    features = _json.loads(features)
                except Exception:
                    features = {}
            _currency = features.get("currency") if isinstance(features, dict) else None

            fiscal_invoice_id = None
            if billing._is_dian_enabled(features):
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

            finalized = await db.db_finalize_check_payment(
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
            if not finalized:
                _claimed = False  # claim already gone (concurrent op) — nothing to release
                raise HTTPException(status_code=409, detail="El check fue modificado por otra operación.")
            _claimed = False
        except HTTPException:
            if _claimed and check_id:
                try:
                    await db.db_release_check(check_id)
                except Exception:
                    log.exception("tables.pos_quick_invoice.release_failed", check_id=check_id)
            raise
        except Exception as e:
            if _claimed and check_id:
                try:
                    await db.db_release_check(check_id)
                except Exception:
                    log.exception("tables.pos_quick_invoice.release_failed", check_id=check_id)
            log.exception("tables.pos_quick_invoice.unexpected_error", order_id=order_id)
            raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")

    return {
        "success": True,
        "order_id": order_id,
        "check_id": check_id,
        "total": float(total_d),
        "fiscal_invoice_id": fiscal_invoice_id,
    }


# ── CAJA: Customer lookup ─────────────────────────────────────────────────────

@router.get("/api/cashier/customer/{phone}")
async def get_cashier_customer(
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
        log.exception("cashier_customer.loyalty_lookup_failed", phone=clean_phone, org_id=org_id)

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
        log.exception("cashier_customer.recent_orders_failed", phone=phone, org_id=org_id)
        return []

    combined = delivery + table
    combined.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return combined[:limit]


# ── CAJA: Recent NPS feed ─────────────────────────────────────────────────────

@router.get("/api/cashier/recent-nps")
async def get_cashier_recent_nps(
    limit: int = 10,
    restaurant: dict = Depends(get_current_restaurant_scoped),
) -> dict:
    """Return recent NPS responses for the caja widget.

    Auth: Bearer token of admin/owner/gerente (get_current_restaurant_scoped).
    Tenant-scoped via the dep — the repo query filters by app.org_id GUC.

    Returns:
        {"items": [{"phone": "***1234", "score": 5, "comment": "...", "created_at": "..."}]}

    Phone is anonymized (last 4 digits only). limit is clamped to [1, 50].
    """
    rows = await db.db_get_recent_nps_for_cashier(limit)
    return {"items": rows}