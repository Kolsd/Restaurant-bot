import os
import json
import csv
import io
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException, File, UploadFile, Depends
from pydantic import BaseModel, Field
from app.services import database as db
from app.repositories.internal import crm_repo
from app.services.logging import get_logger, mask_phone
from app.routes.deps import verify_superadmin
from app.services.provisioning import (
    DEFAULT_TRIAL_DAYS,
    ProvisioningError,
    TenantAlreadyExists,
    create_tenant,
    generate_temp_password,
    username_from,
)

log = get_logger(__name__)

router = APIRouter(prefix="/api/internal/crm", tags=["internal-crm"])

# ── CONFIG ────────────────────────────────────────────────────────────

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



# ── DB HELPERS ────────────────────────────────────────────────────────
# _ser kept as a local alias for backward compat within this module
def _ser(row: dict) -> dict:
    return crm_repo._serialize(row)



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
    limit = max(1, min(limit, 500))  # hard cap
    prospects = await crm_repo.db_get_prospects(
        archived=archived, stage=stage, priority=priority, search=search, limit=limit
    )
    return {"prospects": prospects}

@router.post("/prospects")
async def create_prospect(body: ProspectCreate, _: None = Depends(verify_superadmin)):
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
    """Returns only the date of the last change across the whole table."""
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


class StageMoveRequest(BaseModel):
    stage: str
    lost_reason: Optional[str] = Field(None, max_length=500)


@router.patch("/prospects/{pid}/stage")
async def move_stage(
    pid: int,
    body: StageMoveRequest,
    _: None = Depends(verify_superadmin),
):
    new_stage = body.stage.strip().lower()
    if not new_stage:
        raise HTTPException(status_code=400, detail="stage requerido")
    if new_stage not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"stage inválido. Valores permitidos: {', '.join(sorted(VALID_STAGES))}",
        )
    if new_stage == "perdido" and not body.lost_reason:
        raise HTTPException(
            status_code=400,
            detail="lost_reason requerido al mover a 'perdido'. Indica por qué se perdió el prospecto.",
        )
    await crm_repo.db_move_prospect_stage(pid, new_stage, lost_reason=body.lost_reason)
    return {"success": True, "stage": new_stage}


@router.get("/loss-reasons")
async def get_loss_reasons(_: None = Depends(verify_superadmin)):
    """Top lost_reason counts — used by CRM dashboard analytics."""
    return {"reasons": await crm_repo.db_get_loss_reasons(limit=10)}


# ── CONVERT PROSPECT TO REAL RESTAURANT ───────────────────────────────────────


class ConvertProspectBody(BaseModel):
    """Optional overrides when converting. Defaults pull from the prospect row."""
    name:            Optional[str] = None   # defaults to prospect.restaurant_name
    owner_email:     Optional[str] = None   # defaults to prospect.email — destination for
                                             # the welcome email.
                                             # Lets the founder supply an address at convert
                                             # time even when the CRM row never captured one.
    plan_code:       str = "restaurante"    # CEO decision: default to Restaurante plan
    subscription_plan: str = "restaurante"  # legacy alias kept for compat
    features:        Optional[dict] = None
    skip_welcome_message: bool = False      # founder option to skip the welcome send on convert
    trial_days:      int = DEFAULT_TRIAL_DAYS  # Closed product decision (docs/claude/status.md
                                             # #12, revised 2026-09-23): free days ON TOP of
                                             # the paid plan via organizations.comp_until —
                                             # NOT a plan_code='free', which would have to be
                                             # downgraded later. The number is whatever the
                                             # landing page promises, so it lives in
                                             # services/provisioning and is not repeated here.
                                             # 0 disables the trial for a customer who is
                                             # already paying.


# ── Temp password generator ───────────────────────────────────────────────────
# One implementation, in services/provisioning, because both the CRM convert
# and the self-serve signup hand out credentials. Re-exported under the old
# private name so existing callers and tests keep working.
_generate_temp_password = generate_temp_password


@router.post("/prospects/{pid}/convert")
async def convert_prospect_to_restaurant(
    pid: int,
    body: ConvertProspectBody = ConvertProspectBody(),
    _: None = Depends(verify_superadmin),
):
    """Promote a prospect to a real Mesio organization + primary location.

    What this does:
      1. Reads the prospect row by id.
      2. Validates we have the minimum to create an org (a name).
      3. Delegates org + sede + trial + owner + welcome email to
         services/provisioning.create_tenant — the same code path the
         public self-serve signup runs, so the two cannot drift apart.
      4. Marks the prospect stage='cerrado' and tags it with `org:<id>`.
      5. Adds a system note to the prospect timeline.

    Idempotency:
      The prospect's tags are checked for an existing `org:<id>` first. A
      second convert of an untagged prospect no longer collides on the
      whatsapp_number index (the phone is optional since 2026-09-23), so it
      would create a SECOND organization — the tag check is the guard, not
      the database.

    Returns:
      {
        "ok": True,
        "prospect_id": pid,
        "org_id": int,
        "location_id": int,
        "org": {...},
        "primary_location": {...},
        "user": {"username": str, "temp_password": str},   # shown once to founder
        "welcome_message_sent": bool,
      }

    Security note: temp_password is returned to the founder so they can relay
    credentials if the welcome email fails. It is NOT logged to structlog or Sentry.
    """
    prospect = await crm_repo.db_get_prospect_by_id(pid)
    if not prospect:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")

    # Idempotency hint — if already tagged as converted, refuse with a clear
    # message so the caller can detect it.
    existing_tags = prospect.get("tags") or []
    if any(isinstance(t, str) and t.startswith("org:") for t in existing_tags):
        raise HTTPException(
            status_code=409,
            detail="Prospecto ya fue convertido. Revisar tags para encontrar org_id.",
        )

    name = (body.name or prospect.get("restaurant_name") or "").strip()
    # The prospect's phone is a sales contact, never the restaurant's key:
    # WhatsApp is retired (2026-09-25) and a web org is keyed by id (0096).
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Faltan datos: el prospecto debe tener restaurant_name (o pasarlo en el body).",
        )

    plan = body.plan_code or body.subscription_plan or "restaurante"

    # Steps 1-4 (org → sede → trial → owner → welcome email) are the same
    # sequence the public self-serve signup runs, and they live in
    # services/provisioning so that the two cannot drift apart. What stays
    # here is the part that is genuinely CRM: reading the prospect, and the
    # bookkeeping below.
    prospect_email = (prospect.get("email") or "").strip().lower()
    dest_email = (body.owner_email or prospect_email or "").strip().lower()

    try:
        tenant = await create_tenant(
            restaurant_name=name,
            # Local part of the email, or a slug of the restaurant name —
            # the historical behaviour of this endpoint, kept because the
            # founder reads the username out to the customer.
            username=username_from(prospect_email, name),
            # Generated and returned once, for the founder to relay.
            password=None,
            owner_email=dest_email,
            plan_code=plan,
            trial_days=body.trial_days,
            features=body.features or {},
            send_welcome_email=not body.skip_welcome_message,
            # A sales-assisted conversion must end with a working account
            # even when the username is taken; the founder relays whatever
            # it ended up being.
            allow_username_suffix=True,
        )
    except TenantAlreadyExists as exc:
        log.warning("crm.convert.already_exists", prospect_id=pid, stage=exc.stage)
        raise HTTPException(status_code=409, detail=exc.message) from exc
    except ProvisioningError as exc:
        log.warning("crm.convert.provisioning_failed", prospect_id=pid, stage=exc.stage)
        status = 400 if exc.stage == "validate" else 500
        raise HTTPException(status_code=status, detail=exc.message) from exc

    org            = tenant.org
    primary_loc    = tenant.location
    final_username = tenant.username
    temp_password  = tenant.temp_password
    user_created   = tenant.user_created
    welcome_sent   = tenant.welcome_email_sent
    trial_until    = tenant.comp_until

    # 5. Update the prospect — stage + tag + audit note (best-effort)
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
        note_content = (
            f"Convertido a org #{org['id']} ({name}). "
            f"Usuario: {final_username}. "
            f"Bienvenida (email): {'enviada' if welcome_sent else 'no enviada'}."
        )
        await crm_repo.db_create_prospect_note(
            pid, author="system",
            content=note_content,
            note_type="note",
        )
    except Exception:
        log.exception("crm.convert.note_create_failed", prospect_id=pid, org_id=org["id"])

    # Audit log (best-effort, without password)
    try:
        from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415
        from app.services.tenant_context import bypass_tenant_scope as _bypass  # noqa: PLC0415
        with _bypass("crm_convert_audit_log"):
            await db_log_audit_event(
                actor="crm_convert",
                action="prospect.converted_with_onboarding",
                target_type="organization",
                target_id=str(org["id"]),
                org_id=org["id"],
                payload={
                    "prospect_id": pid,
                    "org_id": org["id"],
                    "user_username": final_username,
                    "welcome_sent": welcome_sent,
                },
            )
    except Exception:
        log.exception("crm.convert.audit_log_failed", prospect_id=pid, org_id=org["id"])

    log.info(
        "crm.convert.success",
        prospect_id=pid,
        org_id=org["id"],
        location_id=primary_loc.get("id"),
        user_created=user_created,
        welcome_sent=welcome_sent,
        # temp_password intentionally omitted from logs
    )

    return {
        "ok":                   True,
        "success":              True,   # backward compat
        "prospect_id":          pid,
        "org_id":               org["id"],
        "location_id":          primary_loc.get("id"),
        "org":                  org,
        "primary_location":     primary_loc,
        "user":                 {
            "username":     final_username,
            "temp_password": temp_password,   # shown once to founder; NOT logged
        } if user_created else None,
        "welcome_message_sent": welcome_sent,
        # None when the founder passed trial_days=0 or the write failed — the
        # caller must be able to tell "no trial" from "trial started".
        "comp_until":           trial_until.isoformat() if trial_until else None,
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


# ── IMPORTACIÓN CSV ───────────────────────────────────────────────────
@router.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...), _: None = Depends(verify_superadmin)):
    
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
    return await crm_repo.db_get_crm_stats()
