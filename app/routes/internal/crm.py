import os
import json
import httpx
import csv
import io
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, File, UploadFile, Depends
from pydantic import BaseModel
from app.services import database as db
from app.repositories.internal import crm_repo
from app.services.logging import get_logger
from app.routes.deps import verify_superadmin

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal/crm", tags=["internal-crm"])

# ── CONFIG ────────────────────────────────────────────────────────────
META_API_VERSION = os.getenv("META_API_VERSION", "v20.0")
CRM_TEMPLATE_LANGUAGE = os.getenv("CRM_TEMPLATE_LANGUAGE", "en")

# NOTE: _require_auth (X-Admin-Key + raw ADMIN_KEY fallback) has been replaced
# by verify_superadmin (Depends) everywhere. All CRM callers must obtain a
# session token via POST /api/internal/admin/login first.
# Breaking change: clients that were passing X-Admin-Key or raw ADMIN_KEY
# directly must now do the login exchange first.

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
    stage: str = None,
    priority: str = None,
    search: str = None,
    archived: bool = False,
    limit: int = 500,
    _: None = Depends(verify_superadmin),
):
    await _ensure_crm_tables()
    limit = max(1, min(limit, 500))  # hard cap
    prospects = await crm_repo.db_get_prospects(
        archived=archived, stage=stage, priority=priority, search=search, limit=limit
    )
    return {"prospects": prospects}

@router.post("/prospects")
async def create_prospect(body: ProspectCreate, _: None = Depends(verify_superadmin)):
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
async def check_updates(_: None = Depends(verify_superadmin)):
    """Devuelve únicamente la fecha del último cambio en toda la tabla."""
    latest = await crm_repo.db_get_prospects_last_updated()
    return {"latest": latest}

@router.patch("/prospects/{pid}")
async def update_prospect(pid: int, body: ProspectUpdate, _: None = Depends(verify_superadmin)):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    result = await crm_repo.db_update_prospect(pid, updates)
    if not result:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")
    return {"success": True, "prospect": result}


@router.delete("/prospects/{pid}")
async def delete_prospect(pid: int, _: None = Depends(verify_superadmin)):
    await crm_repo.db_delete_prospect(pid)
    return {"success": True}


# Valid CRM pipeline stages — must match the dropdowns in static/internal/crm.html.
# Adding a new stage means: update this set + the HTML <option> + the kanban CSS.
VALID_STAGES = frozenset({
    "prospecto",      # cold lead, never contacted
    "contactado",     # outbound message sent, awaiting reply
    "respondio",      # replied at least once
    "demo",           # in active demo / trial
    "negociacion",    # discussing terms
    "cerrado",        # converted (or about to convert) — terminal success
    "perdido",        # lost — terminal failure
})


@router.patch("/prospects/{pid}/stage")
async def move_stage(request: Request, pid: int, _: None = Depends(verify_superadmin)):
    body = await request.json()
    new_stage = (body.get("stage") or "").strip().lower()
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage requerido")
    if new_stage not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage inválido. Valores permitidos: {', '.join(sorted(VALID_STAGES))}",
        )
    await crm_repo.db_move_prospect_stage(pid, new_stage)
    return {"success": True, "stage": new_stage}


# ── CONVERT PROSPECT TO REAL RESTAURANT ───────────────────────────────────────


class ConvertProspectBody(BaseModel):
    """Optional overrides when converting. Defaults pull from the prospect row."""
    name:            Optional[str] = None   # defaults to prospect.restaurant_name
    whatsapp_number: Optional[str] = None   # defaults to prospect.phone
    subscription_plan: str = "free"
    features:        Optional[dict] = None


@router.post("/prospects/{pid}/convert")
async def convert_prospect_to_restaurant(
    pid: int,
    body: ConvertProspectBody = ConvertProspectBody(),
    _: None = Depends(verify_superadmin),
):
    """Promote a prospect to a real Mesio organization + primary location.

    What this does:
      1. Reads the prospect row by id.
      2. Validates we have the minimum to create an org (name + whatsapp).
      3. Calls restaurant_repo.db_create_organization (cross-tenant — uses the
         existing internal-admin bypass already configured on those helpers).
      4. Auto-creates the primary location named 'Principal'.
      5. Marks the prospect stage='cerrado' and tags it with `org:<id>` so
         future CRM views can show "this prospect was converted to org #42"
         without needing a new column / migration.
      6. Adds a system note to the prospect timeline with the new org_id for
         audit traceability.

    Idempotency:
      Calling convert twice on the same prospect WILL fail at the org-create
      step (UNIQUE constraint on whatsapp_number → 409). The caller should
      check the prospect's tags for an existing `org:<id>` tag before retrying.

    Returns:
      {"success": True, "org": {...}, "primary_location": {...}, "prospect_id": pid}
    """
    import asyncpg  # noqa: PLC0415
    from app.repositories import restaurant_repo  # noqa: PLC0415

    prospect = await crm_repo.db_get_prospect_by_id(pid)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")

    # Idempotency hint — if already tagged as converted, refuse with a clear
    # message so the caller can detect (instead of getting a generic 409 from
    # the org create when the whatsapp_number collides).
    existing_tags = prospect.get("tags") or []
    if any(isinstance(t, str) and t.startswith("org:") for t in existing_tags):
        raise HTTPException(
            status_code=409,
            detail="Prospecto ya fue convertido. Revisar tags para encontrar org_id.",
        )

    name = (body.name or prospect.get("restaurant_name") or "").strip()
    wa   = (body.whatsapp_number or prospect.get("phone") or "").strip()
    if not name or not wa:
        raise HTTPException(
            status_code=400,
            detail="Faltan datos: el prospecto debe tener restaurant_name y phone (o pasarlos en el body).",
        )

    # 1. Create the organization
    try:
        org = await restaurant_repo.db_create_organization(
            name=name,
            whatsapp_number=wa,
            features=body.features or {},
            subscription_plan=body.subscription_plan or "free",
        )
    except asyncpg.UniqueViolationError as exc:
        log.warning("crm.convert.unique_violation", prospect_id=pid, detail=str(exc))
        raise HTTPException(
            status_code=409,
            detail="Ya existe una organización con ese teléfono. Verificá si ya fue convertido.",
        )
    except Exception as exc:
        log.exception("crm.convert.org_create_failed", prospect_id=pid)
        raise HTTPException(status_code=500, detail=f"Error creando la org: {str(exc)[:120]}")

    # 2. Create the primary location
    try:
        primary_loc = await restaurant_repo.db_create_location(
            org_id=org["id"],
            name="Principal",
            code="principal",
            active=True,
        )
    except Exception:
        log.exception("crm.convert.location_create_failed", prospect_id=pid, org_id=org["id"])
        # The org was created — surface that to the caller so they can decide
        # whether to manually create the location via the orgs endpoints.
        raise HTTPException(
            status_code=500,
            detail=f"Org #{org['id']} creada, pero falló creación de sede Principal.",
        )

    # 3. Update the prospect — stage + tag + audit note. All best-effort: if
    #    any of these glitch the conversion already succeeded above and the
    #    caller has the org id; we just lose the kanban marker.
    try:
        new_tags = list(existing_tags) + [f"org:{org['id']}"]
        await crm_repo.db_update_prospect(pid, {
            "stage":    "cerrado",
            "tags":     new_tags,
            "archived": False,
        })
    except Exception:
        log.exception("crm.convert.prospect_update_failed", prospect_id=pid, org_id=org["id"])

    try:
        await crm_repo.db_create_prospect_note(
            pid, author="system",
            content=f"Convertido a org #{org['id']} ({name}).",
            note_type="note",
        )
    except Exception:
        log.exception("crm.convert.note_create_failed", prospect_id=pid, org_id=org["id"])

    log.info(
        "crm.convert.success",
        prospect_id=pid,
        org_id=org["id"],
        location_id=primary_loc.get("id"),
    )

    return {
        "success":           True,
        "prospect_id":       pid,
        "org":               org,
        "primary_location":  primary_loc,
    }


# ── NOTES ─────────────────────────────────────────────────────────────
@router.get("/prospects/{pid}/notes")
async def get_notes(pid: int, _: None = Depends(verify_superadmin)):
    notes = await crm_repo.db_get_prospect_notes(pid)
    return {"notes": notes}


@router.post("/prospects/{pid}/notes")
async def add_note(pid: int, body: NoteCreate, _: None = Depends(verify_superadmin)):
    note = await crm_repo.db_create_prospect_note(
        pid, author="mesio_admin",
        content=body.content, note_type=body.note_type,
    )
    return {"success": True, "note": note}


@router.delete("/prospects/{pid}/notes/{nid}")
async def delete_note(pid: int, nid: int, _: None = Depends(verify_superadmin)):
    await crm_repo.db_delete_prospect_note(nid, pid)
    return {"success": True}


# ── INTERACTIONS (historial completo) ─────────────────────────────────
@router.get("/prospects/{pid}/interactions")
async def get_interactions(pid: int, _: None = Depends(verify_superadmin)):
    interactions = await crm_repo.db_get_prospect_interactions(pid)
    return {"interactions": interactions}


# ── SEND WHATSAPP MESSAGE (manual 1:1) ───────────────────────────────
@router.post("/send-message")
async def send_manual_message(body: SendMessagePayload, _: None = Depends(verify_superadmin)):
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
async def send_template(body: SendTemplatePayload, _: None = Depends(verify_superadmin)):
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
async def get_templates(_: None = Depends(verify_superadmin)):
    await _ensure_crm_tables()
    templates = await crm_repo.db_get_crm_templates()
    return {"templates": templates}


@router.post("/templates")
async def create_template(body: TemplateCreate, _: None = Depends(verify_superadmin)):
    try:
        template = await crm_repo.db_create_crm_template(
            name=body.name, wa_name=body.wa_name, language=body.language,
            category=body.category, body=body.body, params=body.params,
        )
        return {"success": True, "template": template}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.delete("/templates/{tid}")
async def delete_template(tid: int, _: None = Depends(verify_superadmin)):
    await crm_repo.db_delete_crm_template(tid)
    return {"success": True}

# ── IMPORTACIÓN CSV ───────────────────────────────────────────────────
@router.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...), _: None = Depends(verify_superadmin)):
    
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
async def crm_stats(_: None = Depends(verify_superadmin)):
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