import os
import json
import httpx
import csv
import io
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, File, UploadFile
from pydantic import BaseModel
from app.services import database as db
from app.repositories.internal import crm_repo
from app.services.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal/crm", tags=["internal-crm"])

# ── CONFIG ────────────────────────────────────────────────────────────
ADMIN_KEY = os.getenv("ADMIN_KEY")
META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")
CRM_TEMPLATE_LANGUAGE = os.getenv("CRM_TEMPLATE_LANGUAGE", "en")

async def _require_auth(request: Request) -> dict:
    from app.repositories import sessions_repo
    key = (
        request.headers.get("X-Admin-Key", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    ).strip()
    if not key:
        raise HTTPException(status_code=401, detail="Acceso exclusivo para el equipo Mesio")
    # Preferred: session token issued by /api/admin/login
    identity = await sessions_repo.get_session(key)
    if identity == "superadmin":
        return {"username": "mesio_admin", "role": "superadmin"}
    # Legacy fallback: raw ADMIN_KEY comparison (for backward compat during transition)
    if ADMIN_KEY and key == ADMIN_KEY:
        return {"username": "mesio_admin", "role": "superadmin"}
    raise HTTPException(status_code=401, detail="Acceso exclusivo para el equipo Mesio")

# ── MODELOS ───────────────────────────────────────────────────────────
class ProspectCreate(BaseModel):
    restaurant_name: str
    owner_name:      str = ""
    phone:           str
    city:            str = ""
    neighborhood:    str = ""
    category:        str = ""
    instagram:       str = ""
    google_maps:     str = ""
    source:          str = "manual"
    stage:           str = "prospecto"
    priority:        str = "medium"
    revenue_est:     int = 0
    tags:            List[str] = []

class ProspectUpdate(BaseModel):
    restaurant_name: Optional[str] = None
    owner_name:      Optional[str] = None
    phone:           Optional[str] = None
    city:            Optional[str] = None
    neighborhood:    Optional[str] = None
    category:        Optional[str] = None
    instagram:       Optional[str] = None
    google_maps:     Optional[str] = None
    stage:           Optional[str] = None
    priority:        Optional[str] = None
    revenue_est:     Optional[int] = None
    tags:            Optional[List[str]] = None
    next_follow_up:  Optional[str] = None
    archived:        Optional[bool] = None

class NoteCreate(BaseModel):
    content:   str
    note_type: str = "note"   # note | call | email | whatsapp | meeting

class TemplateCreate(BaseModel):
    name:     str
    wa_name:  str
    language: str = "es_CO"
    category: str = "MARKETING"
    body:     str
    params:   List[str] = []

class SendTemplatePayload(BaseModel):
    prospect_ids:  List[int]
    template_id:   int
    params_map:    dict = {}   # {prospect_id: [param1, param2, ...]}

class SendMessagePayload(BaseModel):
    prospect_id: int
    message:     str


# ── DB HELPERS ────────────────────────────────────────────────────────
# _ser kept as a local alias for backward compat within this module
def _ser(row: dict) -> dict:
    return crm_repo._serialize(row)


async def _ensure_crm_tables():
    # Schema managed by Alembic. This is seed-only for default templates.
    await crm_repo.db_seed_crm_templates()

# ── PROSPECTS CRUD ────────────────────────────────────────────────────
@router.get("/prospects")
async def get_prospects(
    request: Request,
    stage: str = None,
    priority: str = None,
    search: str = None,
    archived: bool = False,
    limit: int = 500,
):
    await _require_auth(request)
    await _ensure_crm_tables()
    limit = max(1, min(limit, 500))  # hard cap
    prospects = await crm_repo.db_get_prospects(
        archived=archived, stage=stage, priority=priority, search=search, limit=limit
    )
    return {"prospects": prospects}

@router.post("/prospects")
async def create_prospect(request: Request, body: ProspectCreate):
    await _require_auth(request)
    await _ensure_crm_tables()
    prospect = await crm_repo.db_create_prospect(
        restaurant_name=body.restaurant_name, owner_name=body.owner_name,
        phone=body.phone, city=body.city, neighborhood=body.neighborhood,
        category=body.category, instagram=body.instagram, google_maps=body.google_maps,
        source=body.source, stage=body.stage, priority=body.priority,
        revenue_est=body.revenue_est, tags=body.tags,
    )
    return {"success": True, "prospect": prospect}

@router.get("/check-updates")
async def check_updates(request: Request):
    """Devuelve únicamente la fecha del último cambio en toda la tabla."""
    await _require_auth(request)
    latest = await crm_repo.db_get_prospects_last_updated()
    return {"latest": latest}

@router.patch("/prospects/{pid}")
async def update_prospect(request: Request, pid: int, body: ProspectUpdate):
    await _require_auth(request)
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    result = await crm_repo.db_update_prospect(pid, updates)
    if not result:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")
    return {"success": True, "prospect": result}


@router.delete("/prospects/{pid}")
async def delete_prospect(request: Request, pid: int):
    await _require_auth(request)
    await crm_repo.db_delete_prospect(pid)
    return {"success": True}


@router.patch("/prospects/{pid}/stage")
async def move_stage(request: Request, pid: int):
    await _require_auth(request)
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage requerido")
    await crm_repo.db_move_prospect_stage(pid, new_stage)
    return {"success": True}


# ── NOTES ─────────────────────────────────────────────────────────────
@router.get("/prospects/{pid}/notes")
async def get_notes(request: Request, pid: int):
    await _require_auth(request)
    notes = await crm_repo.db_get_prospect_notes(pid)
    return {"notes": notes}


@router.post("/prospects/{pid}/notes")
async def add_note(request: Request, pid: int, body: NoteCreate):
    user = await _require_auth(request)
    note = await crm_repo.db_create_prospect_note(
        pid, author=user.get("username", "admin"),
        content=body.content, note_type=body.note_type,
    )
    return {"success": True, "note": note}


@router.delete("/prospects/{pid}/notes/{nid}")
async def delete_note(request: Request, pid: int, nid: int):
    await _require_auth(request)
    await crm_repo.db_delete_prospect_note(nid, pid)
    return {"success": True}


# ── INTERACTIONS (historial completo) ─────────────────────────────────
@router.get("/prospects/{pid}/interactions")
async def get_interactions(request: Request, pid: int):
    await _require_auth(request)
    interactions = await crm_repo.db_get_prospect_interactions(pid)
    return {"interactions": interactions}


# ── SEND WHATSAPP MESSAGE (manual 1:1) ───────────────────────────────
@router.post("/send-message")
async def send_manual_message(request: Request, body: SendMessagePayload):
    await _require_auth(request)
    prospect = await crm_repo.db_get_prospect_by_id(body.prospect_id)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")

    prospect = dict(prospect)
    phone    = prospect["phone"].lstrip("+").replace(" ", "")
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = os.getenv("CRM_PHONE_NUMBER_ID") or os.getenv("META_PHONE_NUMBER_ID", "")  # CRM usa número de prospectos

    wa_msg_id = ""
    status    = "sent"
    error_msg = ""

    if token and phone_id:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": phone,
                        "type": "text",
                        "text": {"body": body.message}
                    }
                )
                data = resp.json()
                if resp.status_code == 200:
                    wa_msg_id = data.get("messages", [{}])[0].get("id", "")
                else:
                    status    = "error"
                    error_msg = data.get("error", {}).get("message", str(resp.text[:200]))
        except Exception as e:
            status    = "error"
            error_msg = str(e)[:200]
    else:
        status    = "no_credentials"
        error_msg = "Configura CRM_PHONE_NUMBER_ID en Railway con el ID del número de prospectos"

    # Registrar la interacción SOLO si se envió con éxito
    if status == "sent":
        await crm_repo.db_record_outbound_interaction(
            body.prospect_id, content=body.message, wa_message_id=wa_msg_id
        )

    if status == "error":
        raise HTTPException(status_code=422, detail=error_msg)

    return {"success": True, "status": status, "wa_message_id": wa_msg_id}

# ── SEND TEMPLATE (masivo) ────────────────────────────────────────────
@router.post("/send-template")
async def send_template(request: Request, body: SendTemplatePayload):
    await _require_auth(request)
    tpl = await crm_repo.db_get_crm_template_by_id(body.template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template no encontrado")

    tpl      = dict(tpl)
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = os.getenv("CRM_PHONE_NUMBER_ID") or os.getenv("META_PHONE_NUMBER_ID", "")

    _PROSPECT_FIELDS = {
        "restaurante": "restaurant_name", "restaurant": "restaurant_name",
        "nombre":      "owner_name",      "name":       "owner_name",
        "ciudad":      "city",            "city":       "city",
    }

    results = []
    for pid in body.prospect_ids:
        prospect = await crm_repo.db_get_prospect_by_id(pid)
        if not prospect:
            results.append({"prospect_id": pid, "status": "not_found"})
            continue
        phone    = prospect["phone"].lstrip("+").replace(" ", "")

        # Build template components resolviendo parámetros desde el prospecto
        components = []
        tpl_param_names = [str(p).strip().lower() for p in (tpl.get("params") or [])]

        if tpl_param_names:
            parameters_list = []
            for p_name in tpl_param_names:
                field    = _PROSPECT_FIELDS.get(p_name)
                resolved = str(prospect[field]) if field and prospect.get(field) else ""
                if not resolved:
                    continue  # omitir parámetro si no hay dato
                clean_text = resolved.replace("{", "").replace("}", "")
                param_obj  = {"type": "text", "text": clean_text}
                if not p_name.isdigit():
                    param_obj["parameter_name"] = p_name[:20]
                parameters_list.append(param_obj)
                
            components.append({
                "type": "body",
                "parameters": parameters_list
            })

        wa_msg_id = ""
        status    = "sent"
        error_msg = ""

        if token and phone_id:
            try:
                meta_payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "template",
                    "template": {
                        "name": tpl["wa_name"],
                        "language": {
                            "policy": "deterministic",
                            "code": tpl.get("language") or CRM_TEMPLATE_LANGUAGE
                        },
                        "components": components
                    }
                }
                log.info("crm.template_sending", phone=phone, template=tpl["wa_name"])

                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"https://graph.facebook.com/{META_API_VERSION}/{phone_id}/messages",
                        headers={"Authorization": f"Bearer {token}"},
                        json=meta_payload
                    )
                    data = resp.json()
                    log.info("crm.template_response", phone=phone, status=resp.status_code)

                    if resp.status_code == 200:
                        wa_msg_id = data.get("messages", [{}])[0].get("id", "")
                    else:
                        status    = "error"
                        error_msg = data.get("error", {}).get("message", str(resp.text[:200]))
            except Exception as e:
                status    = "error"
                error_msg = str(e)[:200]
                log.error("crm.template_send_failed", phone=phone, error=error_msg)
        else:
            status    = "no_credentials"
            error_msg = "Credenciales Meta no configuradas"

        # Construir preview reemplazando parámetros con valores del prospecto
        preview = tpl["body"]
        for p_name in tpl_param_names:
            field    = _PROSPECT_FIELDS.get(p_name)
            resolved = str(prospect[field]) if field and prospect.get(field) else ""
            if resolved:
                preview = preview.replace("{{" + p_name + "}}", resolved)

        # Registrar en la base de datos SOLO si se envió con éxito
        if status == "sent":
            await crm_repo.db_record_outbound_interaction(
                pid, content=preview, template_name=tpl["wa_name"], wa_message_id=wa_msg_id
            )

        results.append({
            "prospect_id": pid,
            "phone":       phone,
            "status":      status,
            "error":       error_msg,
            "wa_msg_id":   wa_msg_id
        })

    sent_ok  = len([r for r in results if r["status"] == "sent"])
    sent_err = len([r for r in results if r["status"] == "error"])
    return {
        "success":   True,
        "total":     len(results),
        "sent":      sent_ok,
        "errors":    sent_err,
        "results":   results
    }


# ── TEMPLATES CRUD ────────────────────────────────────────────────────
@router.get("/templates")
async def get_templates(request: Request):
    await _require_auth(request)
    await _ensure_crm_tables()
    templates = await crm_repo.db_get_crm_templates()
    return {"templates": templates}


@router.post("/templates")
async def create_template(request: Request, body: TemplateCreate):
    await _require_auth(request)
    try:
        template = await crm_repo.db_create_crm_template(
            name=body.name, wa_name=body.wa_name, language=body.language,
            category=body.category, body=body.body, params=body.params,
        )
        return {"success": True, "template": template}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/templates/{tid}")
async def delete_template(request: Request, tid: int):
    await _require_auth(request)
    await crm_repo.db_delete_crm_template(tid)
    return {"success": True}

# ── IMPORTACIÓN CSV ───────────────────────────────────────────────────
@router.post("/upload-csv")
async def upload_csv(request: Request, file: UploadFile = File(...)):
    await _require_auth(request)
    
    await _ensure_crm_tables()
    content = await file.read()

    # utf-8-sig strips Excel BOM (\ufeff); fallback to latin-1 for Windows-1252
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))

    inserted = 0
    errors = 0

    for row in reader:
        def _col(*keys): return next((row.get(k,'') for k in keys if row.get(k,'')), '').strip()
        name  = _col('Restaurante','restaurante','name')
        phone = _col('Telefono','telefono','phone')
        owner = _col('Dueño','Dueno','owner')
        city  = _col('Ciudad','city')
        neighborhood = _col('Barrio','barrio','neighborhood')
        category     = _col('Categoria','categoria','category')
        instagram    = _col('Instagram','instagram')
        google_maps  = _col('Google Maps','google_maps')
        source       = _col('Fuente','fuente','source') or 'csv_import'
        stage        = _col('Etapa Inicial','etapa_inicial','stage') or 'prospecto'
        priority     = _col('Prioridad','prioridad','priority') or 'medium'

        if not name or not phone:
            errors += 1
            continue

        phone = phone.replace(" ", "").replace("+", "").replace("-", "")

        try:
            was_inserted = await crm_repo.db_upsert_prospect_from_csv(
                name=name, owner=owner, phone=phone, city=city,
                neighborhood=neighborhood, category=category,
                instagram=instagram, google_maps=google_maps,
                source=source, stage=stage, priority=priority,
            )
            if was_inserted:
                inserted += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    return {"success": True, "inserted": inserted, "errors": errors}

# ── STATS / KANBAN COUNTS ─────────────────────────────────────────────
@router.get("/stats")
async def crm_stats(request: Request):
    await _require_auth(request)
    await _ensure_crm_tables()
    return await crm_repo.db_get_crm_stats()

# ── PAGE ROUTE ────────────────────────────────────────────────────────
from fastapi import Response as FResponse
from pathlib import Path
from fastapi.responses import HTMLResponse

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def crm_page():
    p = Path(__file__).parent.parent / "static" / "crm.html"
    if p.exists():
        return HTMLResponse(p.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>crm.html no encontrado en static/</h1>", status_code=404)

# ── INBOUND WEBHOOK HOOK — registra respuestas de prospectos ─────────
async def register_inbound_from_prospect(phone: str, message: str, wa_message_id: str = ""):
    """
    Llamado desde chat.py cuando llega un mensaje de WhatsApp.
    Si el número no existe, lo crea. Si existe, registra la interacción.
    Delegates all SQL to crm_repo.db_record_inbound_interaction.
    """
    await crm_repo.db_record_inbound_interaction(phone, message, wa_message_id)