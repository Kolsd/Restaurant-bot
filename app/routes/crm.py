import os
import json
import httpx
import csv
import io
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, File, UploadFile
from pydantic import BaseModel
from app.services.auth import verify_token
from app.services import database as db

router = APIRouter(prefix="/api/crm", tags=["crm"])

# ── AUTH HELPER ───────────────────────────────────────────────────────
ADMIN_KEY = os.getenv("ADMIN_KEY") # <-- Sin contraseña por defecto

async def _require_auth(request: Request) -> dict:
    key = (
        request.headers.get("X-Admin-Key", "")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    )
    # Bloqueamos si el servidor no tiene llave o si la llave es incorrecta
    if not ADMIN_KEY or not key or key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Acceso exclusivo para el equipo Mesio")
    return {"username": "mesio_admin", "role": "superadmin"}

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
def _ser(row: dict) -> dict:
    result = {}
    for k, v in row.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()[:19] + "Z"
        elif isinstance(v, list):
            result[k] = list(v)
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    return result


async def _ensure_crm_tables():
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prospects (
                id            SERIAL PRIMARY KEY,
                restaurant_name TEXT    NOT NULL,
                owner_name      TEXT    NOT NULL DEFAULT '',
                phone           TEXT    NOT NULL,
                city            TEXT    NOT NULL DEFAULT '',
                neighborhood    TEXT    NOT NULL DEFAULT '',
                category        TEXT    NOT NULL DEFAULT '',
                instagram       TEXT    NOT NULL DEFAULT '',
                google_maps     TEXT    NOT NULL DEFAULT '',
                source          TEXT    NOT NULL DEFAULT 'manual',
                stage           TEXT    NOT NULL DEFAULT 'prospecto',
                priority        TEXT    NOT NULL DEFAULT 'medium',
                assigned_to     TEXT    NOT NULL DEFAULT '',
                last_contact_at TIMESTAMP,
                next_follow_up  TIMESTAMP,
                revenue_est     INTEGER DEFAULT 0,
                tags            TEXT[]  DEFAULT '{}',
                archived        BOOLEAN DEFAULT FALSE,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS prospect_notes (
                id          SERIAL PRIMARY KEY,
                prospect_id INTEGER NOT NULL,
                author      TEXT    NOT NULL DEFAULT 'miguel',
                content     TEXT    NOT NULL,
                note_type   TEXT    NOT NULL DEFAULT 'note',
                created_at  TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS prospect_interactions (
                id            SERIAL PRIMARY KEY,
                prospect_id   INTEGER NOT NULL,
                direction     TEXT    NOT NULL DEFAULT 'outbound',
                channel       TEXT    NOT NULL DEFAULT 'whatsapp',
                content       TEXT    NOT NULL,
                template_name TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'sent',
                wa_message_id TEXT    NOT NULL DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS crm_templates (
                id          SERIAL PRIMARY KEY,
                name        TEXT    NOT NULL UNIQUE,
                wa_name     TEXT    NOT NULL DEFAULT '',
                category    TEXT    NOT NULL DEFAULT 'MARKETING',
                language    TEXT    NOT NULL DEFAULT 'es',
                body        TEXT    NOT NULL,
                params      TEXT[]  DEFAULT '{}',
                active      BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMP DEFAULT NOW()
            );
        """)
        
        # 👇 NUEVO: Sembrar templates por defecto SOLO si la tabla está completamente vacía
        count = await conn.fetchval("SELECT COUNT(*) FROM crm_templates")
        if count == 0:
            await conn.execute("""
                INSERT INTO crm_templates (name, wa_name, category, body, params)
                VALUES
                  ('Prospección inicial', 'mesio_prospeccion_v1', 'MARKETING',
                   'Hola {{1}}, vi que tienen {{2}} y quería hacerles una pregunta rápida — ¿reciben pedidos por WhatsApp o solo por Rappi? Tenemos algo que podría ahorrarles la comisión. 🙋',
                   ARRAY['nombre del dueño','nombre del restaurante']),
                  ('Follow-up demo', 'mesio_followup_demo_v1', 'MARKETING',
                   'Hola {{1}}! Les comparto el demo de Mesio para que vean cómo funcionaría para {{2}}: mesioai.com/demo — ¿tienen 15 minutos esta semana para una llamada rápida?',
                   ARRAY['nombre','restaurante']),
                  ('Cierre', 'mesio_cierre_v1', 'MARKETING',
                   'Hola {{1}}, quería saber si pudieron ver el demo de Mesio. Tenemos el plan Starter desde $49 USD/mes y podemos tenerlo configurado en 48h. ¿Arrancamos esta semana?',
                   ARRAY['nombre'])
                ON CONFLICT (name) DO NOTHING;
            """)

# ── PROSPECTS CRUD ────────────────────────────────────────────────────
@router.get("/prospects")
async def get_prospects(
    request: Request,
    stage: str = None,
    priority: str = None,
    search: str = None,
    archived: bool = False,
    limit: int = 10000  # 👈 Límite aumentado a 10,000 para evitar que desaparezcan
):
    await _require_auth(request)
    await _ensure_crm_tables()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        conditions = ["archived = $1"]
        params: list = [archived]
        idx = 2

        if stage:
            conditions.append(f"stage = ${idx}"); params.append(stage); idx += 1
        if priority:
            conditions.append(f"priority = ${idx}"); params.append(priority); idx += 1
        if search:
            conditions.append(f"(restaurant_name ILIKE ${idx} OR owner_name ILIKE ${idx} OR phone ILIKE ${idx})")
            params.append(f"%{search}%"); idx += 1

        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"SELECT * FROM prospects WHERE {where} ORDER BY updated_at DESC LIMIT {limit}",
            *params
        )
        return {"prospects": [_ser(dict(r)) for r in rows]}

@router.post("/prospects")
async def create_prospect(request: Request, body: ProspectCreate):
    await _require_auth(request)
    await _ensure_crm_tables()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO prospects
              (restaurant_name, owner_name, phone, city, neighborhood, category,
               instagram, google_maps, source, stage, priority, revenue_est, tags)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            RETURNING *
        """, body.restaurant_name, body.owner_name, body.phone, body.city,
             body.neighborhood, body.category, body.instagram, body.google_maps,
             body.source, body.stage, body.priority, body.revenue_est, body.tags)
        return {"success": True, "prospect": _ser(dict(row))}

@router.get("/check-updates")
async def check_updates(request: Request):
    """Devuelve únicamente la fecha del último cambio en toda la tabla."""
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        latest = await conn.fetchval("SELECT MAX(updated_at) FROM prospects")
        if latest:
            return {"latest": latest.isoformat()[:19] + "Z"}
        return {"latest": None}

@router.patch("/prospects/{pid}")
async def update_prospect(request: Request, pid: int, body: ProspectUpdate):
    await _require_auth(request)
    pool = await db.get_pool()
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="Nada que actualizar")

    updates["updated_at"] = datetime.utcnow()

    set_clauses = [f"{k} = ${i+2}" for i, k in enumerate(updates.keys())]
    values = [pid] + list(updates.values())

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE prospects SET {', '.join(set_clauses)} WHERE id=$1 RETURNING *",
            *values
        )
        if not row:
            raise HTTPException(status_code=404, detail="Prospecto no encontrado")
        return {"success": True, "prospect": _ser(dict(row))}


@router.delete("/prospects/{pid}")
async def delete_prospect(request: Request, pid: int):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM prospects WHERE id=$1", pid)
    return {"success": True}


@router.patch("/prospects/{pid}/stage")
async def move_stage(request: Request, pid: int):
    await _require_auth(request)
    body = await request.json()
    new_stage = body.get("stage")
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage requerido")
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE prospects SET stage=$2, updated_at=NOW() WHERE id=$1",
            pid, new_stage
        )
    return {"success": True}


# ── NOTES ─────────────────────────────────────────────────────────────
@router.get("/prospects/{pid}/notes")
async def get_notes(request: Request, pid: int):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM prospect_notes WHERE prospect_id=$1 ORDER BY created_at DESC",
            pid
        )
        return {"notes": [_ser(dict(r)) for r in rows]}


@router.post("/prospects/{pid}/notes")
async def add_note(request: Request, pid: int, body: NoteCreate):
    user = await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO prospect_notes (prospect_id, author, content, note_type)
            VALUES ($1,$2,$3,$4) RETURNING *
        """, pid, user.get("username", "miguel"), body.content, body.note_type)
        # Update prospect last contact
        await conn.execute(
            "UPDATE prospects SET last_contact_at=NOW(), updated_at=NOW() WHERE id=$1",
            pid
        )
    return {"success": True, "note": _ser(dict(row))}


@router.delete("/prospects/{pid}/notes/{nid}")
async def delete_note(request: Request, pid: int, nid: int):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM prospect_notes WHERE id=$1 AND prospect_id=$2", nid, pid)
    return {"success": True}


# ── INTERACTIONS (historial completo) ─────────────────────────────────
@router.get("/prospects/{pid}/interactions")
async def get_interactions(request: Request, pid: int):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM prospect_interactions WHERE prospect_id=$1 ORDER BY created_at ASC",
            pid
        )
        return {"interactions": [_ser(dict(r)) for r in rows]}


# ── SEND WHATSAPP MESSAGE (manual 1:1) ───────────────────────────────
@router.post("/send-message")
async def send_manual_message(request: Request, body: SendMessagePayload):
    await _require_auth(request)
    pool = await db.get_pool()

    async with pool.acquire() as conn:
        prospect = await conn.fetchrow("SELECT * FROM prospects WHERE id=$1", body.prospect_id)
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
                    f"https://graph.facebook.com/v20.0/{phone_id}/messages",
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
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO prospect_interactions
                  (prospect_id, direction, channel, content, status, wa_message_id)
                VALUES ($1,'outbound','whatsapp',$2,$3,$4)
            """, body.prospect_id, body.message, status, wa_msg_id)
            await conn.execute(
                "UPDATE prospects SET last_contact_at=NOW(), updated_at=NOW() WHERE id=$1",
                body.prospect_id
            )

    if status == "error":
        raise HTTPException(status_code=422, detail=error_msg)

    return {"success": True, "status": status, "wa_message_id": wa_msg_id}

# ── SEND TEMPLATE (masivo) ────────────────────────────────────────────
@router.post("/send-template")
async def send_template(request: Request, body: SendTemplatePayload):
    await _require_auth(request)
    pool = await db.get_pool()

    async with pool.acquire() as conn:
        tpl = await conn.fetchrow("SELECT * FROM crm_templates WHERE id=$1", body.template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail="Template no encontrado")

    tpl      = dict(tpl)
    token    = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = os.getenv("CRM_PHONE_NUMBER_ID") or os.getenv("META_PHONE_NUMBER_ID", "")

    results = []
    for pid in body.prospect_ids:
        async with pool.acquire() as conn:
            prospect = await conn.fetchrow("SELECT * FROM prospects WHERE id=$1", pid)
        if not prospect:
            results.append({"prospect_id": pid, "status": "not_found"})
            continue

        prospect = dict(prospect)
        phone    = prospect["phone"].lstrip("+").replace(" ", "")
        params   = body.params_map.get(str(pid), [])

        # Build template components
        components = []
        
        # Solo enviamos el cuerpo si hay parámetros
        if params:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in params]
            })

        wa_msg_id = ""
        status    = "sent"
        error_msg = ""

        if token and phone_id:
            try:
                # 👇 LOGS DETALLADOS PARA DEBUGGEAR META
                meta_payload = {
                    "messaging_product": "whatsapp",
                    "to": phone,
                    "type": "template",
                    "template": {
                        "name": tpl["wa_name"],
                        "language": {
                            "policy": "deterministic", # 👈 Obliga a Meta a buscar exacto
                            "code": "es_MX"
                        },
                        "components": components
                    }
                }
                print(f"\n🚀 [ENVIANDO TEMPLATE a {phone}] Payload:\n{json.dumps(meta_payload, indent=2)}", flush=True)

                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"https://graph.facebook.com/v20.0/{phone_id}/messages",
                        headers={"Authorization": f"Bearer {token}"},
                        json=meta_payload
                    )
                    data = resp.json()
                    print(f"📥 [RESPUESTA META]:\n{json.dumps(data, indent=2)}\n", flush=True)

                    if resp.status_code == 200:
                        wa_msg_id = data.get("messages", [{}])[0].get("id", "")
                    else:
                        status    = "error"
                        error_msg = data.get("error", {}).get("message", str(resp.text[:200]))
            except Exception as e:
                status    = "error"
                error_msg = str(e)[:200]
                print(f"❌ [ERROR INTERNO]: {error_msg}", flush=True)
        else:
            status    = "no_credentials"
            error_msg = "Credenciales Meta no configuradas"

        # Build preview of message with params filled in
        preview = tpl["body"]
        for i, p in enumerate(params):
            preview = preview.replace(f"{{{{{i+1}}}}}", str(p))

        # Registrar en la base de datos SOLO si se envió con éxito
        if status == "sent":
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO prospect_interactions
                      (prospect_id, direction, channel, content, template_name, status, wa_message_id)
                    VALUES ($1,'outbound','whatsapp',$2,$3,$4,$5)
                """, pid, preview, tpl["wa_name"], status, wa_msg_id)

                # Auto-advance stage from prospecto → contactado
                await conn.execute("""
                    UPDATE prospects
                    SET last_contact_at=NOW(), updated_at=NOW(),
                        stage = CASE WHEN stage='prospecto' THEN 'contactado' ELSE stage END
                    WHERE id=$1
                """, pid)

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
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM crm_templates ORDER BY id")
        return {"templates": [_ser(dict(r)) for r in rows]}


@router.post("/templates")
async def create_template(request: Request, body: TemplateCreate):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO crm_templates (name, wa_name, category, body, params)
            VALUES ($1,$2,$3,$4,$5) RETURNING *
        """, body.name, body.wa_name, body.category, body.body, body.params)
        return {"success": True, "template": _ser(dict(row))}


@router.delete("/templates/{tid}")
async def delete_template(request: Request, tid: int):
    await _require_auth(request)
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM crm_templates WHERE id=$1", tid)
    return {"success": True}

# ── IMPORTACIÓN CSV ───────────────────────────────────────────────────
@router.post("/upload-csv")
async def upload_csv(request: Request, file: UploadFile = File(...)):
    await _require_auth(request)
    
    content = await file.read()
    try:
                # 🔥 FIX: Búsqueda exacta indexada en lugar de ILIKE
                existing = await conn.fetchval(
                    "SELECT id FROM prospects WHERE phone = $1", 
                    phone
                )
                
                # Solo inserta si no existe previamente
                if not existing:
                    await conn.execute("""
                        INSERT INTO prospects (restaurant_name, owner_name, phone, city, source)
                        VALUES ($1, $2, $3, $4, 'csv_import')
                    """, name, owner, phone, city)
                    inserted += 1
                else:
                    errors += 1 
            except Exception as e:
                errors += 1
    pool = await db.get_pool()
    inserted = 0
    errors = 0

    async with pool.acquire() as conn:
        for row in reader:
            name = row.get('Restaurante', row.get('restaurante', row.get('name', ''))).strip()
            phone = row.get('Telefono', row.get('telefono', row.get('phone', ''))).strip()
            owner = row.get('Dueño', row.get('owner', '')).strip()
            city = row.get('Ciudad', row.get('city', '')).strip()

            if not name or not phone:
                errors += 1
                continue

            phone = phone.replace(" ", "").replace("+", "").replace("-", "")

            try:
                # 👇 Verificamos si el teléfono (los últimos 10 dígitos) ya existe
                existing = await conn.fetchval(
                    "SELECT id FROM prospects WHERE phone ILIKE $1", 
                    f"%{phone[-10:]}%"
                )
                
                # Solo inserta si no existe previamente
                if not existing:
                    await conn.execute("""
                        INSERT INTO prospects (restaurant_name, owner_name, phone, city, source)
                        VALUES ($1, $2, $3, $4, 'csv_import')
                    """, name, owner, phone, city)
                    inserted += 1
                else:
                    # Si ya existe, lo cuenta como error/omitido para no duplicar
                    errors += 1 
            except Exception as e:
                errors += 1

    return {"success": True, "inserted": inserted, "errors": errors}
# ── STATS / KANBAN COUNTS ─────────────────────────────────────────────
@router.get("/stats")
async def crm_stats(request: Request):
    await _require_auth(request)
    await _ensure_crm_tables()
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT stage, COUNT(*) as cnt
            FROM prospects WHERE archived=FALSE
            GROUP BY stage
        """)
        stage_counts = {r["stage"]: r["cnt"] for r in rows}

        total       = await conn.fetchval("SELECT COUNT(*) FROM prospects WHERE archived=FALSE")
        
        # 👇 NUEVO: Cuenta prospectos únicos, no archivados, que recibieron al menos 1 mensaje exitoso
        contacted   = await conn.fetchval("""
            SELECT COUNT(DISTINCT p.id) 
            FROM prospects p
            JOIN prospect_interactions pi ON p.id = pi.prospect_id
            WHERE p.archived=FALSE 
              AND pi.direction='outbound' 
              AND pi.status='sent'
        """)
        
        converted   = stage_counts.get("cerrado", 0)
        follow_ups  = await conn.fetchval("""
            SELECT COUNT(*) FROM prospects
            WHERE next_follow_up <= NOW() + INTERVAL '24 hours'
            AND next_follow_up >= NOW()
            AND archived=FALSE
        """)

    return {
        "stage_counts": stage_counts,
        "total":        total or 0,
        "contacted":    contacted or 0,
        "converted":    converted,
        "follow_ups":   follow_ups or 0,
        "conversion_rate": round((converted / total * 100) if total else 0, 1)
    }

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
    try:
        pool = await db.get_pool()
        # Limpiamos el número de Meta (que ya viene con código de país)
        clean = phone.lstrip("+").replace(" ", "")
        
        async with pool.acquire() as conn:
            # 🔥 FIX: Búsqueda exacta y rápida usando el índice de la BD
            row = await conn.fetchrow(
                "SELECT id, stage FROM prospects WHERE phone = $1 ORDER BY id DESC LIMIT 1",
                clean
            )
            
            # 👇 Si no existe, creamos un prospecto entrante
            if not row:
                print(f"👤 Prospecto no encontrado para {clean}. Creando uno nuevo...", flush=True)
                row = await conn.fetchrow("""
                    INSERT INTO prospects (restaurant_name, phone, source, stage)
                    VALUES ($1, $2, 'inbound_whatsapp', 'respondio')
                    RETURNING id, stage
                """, f"Nuevo Inbound (+{clean[-4:]})", clean)            
            pid   = row["id"]
            stage = row["stage"]

            # Guardamos el mensaje en el historial
            await conn.execute("""
                INSERT INTO prospect_interactions
                  (prospect_id, direction, channel, content, status, wa_message_id)
                VALUES ($1,'inbound','whatsapp',$2,'received',$3)
            """, pid, message, wa_message_id)

            # 2. 🔥 FIX: Actualizamos la etapa a 'respondio' si es un prospecto frío, 
            # o si estaba en PERDIDO / CERRADO (el cliente se arrepintió o volvió)
            new_stage = "respondio" if stage in ("prospecto", "contactado", "perdido", "cerrado") else stage
            
            # 3. Forzamos archived=FALSE por si el prospecto estaba en la papelera
            await conn.execute("""
                UPDATE prospects
                SET last_contact_at=NOW(), updated_at=NOW(), stage=$2, archived=FALSE
                WHERE id=$1
            """, pid, new_stage)
            
            print(f"✅ CRM: Mensaje guardado en el prospecto ID {pid}, etapa movida a {new_stage}", flush=True)

    except Exception as e:
        print(f"⚠️ CRM inbound hook error: {e}", flush=True)