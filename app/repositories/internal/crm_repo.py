"""
CRM repository — prospects, notes, interactions, templates.

Extracted from app.routes.crm (routes-had-SQL anti-pattern fix).
All SQL verbatim from the original route handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessors — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(row: dict) -> dict:
    result = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            result[k] = v.isoformat()[:19] + "Z"
        elif isinstance(v, list):
            result[k] = list(v)
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    return result


# ── SEED ──────────────────────────────────────────────────────────────────────

async def db_seed_crm_templates() -> None:
    """Insert default CRM templates if the table is empty. Idempotent via ON CONFLICT."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM crm_templates")
        if count == 0:
            await conn.execute("""
                INSERT INTO crm_templates (name, wa_name, category, body, params)
                VALUES
                  ('Prospección inicial', 'mesio_prospeccion_v1', 'MARKETING',
                   'Hola {{1}}, vi que tienen {{2}} y quería hacerles una pregunta rápida — ¿reciben pedidos por WhatsApp o solo por Rappi? Tenemos algo que podría ahorrarles la comisión. 🙋',
                   ARRAY['nombre del dueño','nombre del restaurante']),
                  ('Follow-up demo', 'mesio_followup_demo_v1', 'MARKETING',
                   'Hi {{1}}! Here is the Mesio demo so you can see how it would work for {{2}}: {{3}} — Do you have 15 minutes this week for a quick call?',
                   ARRAY['name','restaurant','demo_url']),
                  ('Cierre', 'mesio_cierre_v1', 'MARKETING',
                   'Hola {{1}}, quería saber si pudieron ver el demo de Mesio. Tenemos el plan Starter desde $49 USD/mes y podemos tenerlo configurado en 48h. ¿Arrancamos esta semana?',
                   ARRAY['nombre'])
                ON CONFLICT (name) DO NOTHING;
            """)


# ── PROSPECTS ─────────────────────────────────────────────────────────────────

async def db_get_prospects(
    archived: bool = False,
    stage: Optional[str] = None,
    priority: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 500,
) -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        conditions = ["archived = $1"]
        params: list = [archived]
        idx = 2

        if stage:
            conditions.append(f"stage = ${idx}")
            params.append(stage)
            idx += 1
        if priority:
            conditions.append(f"priority = ${idx}")
            params.append(priority)
            idx += 1
        if search:
            conditions.append(
                f"(restaurant_name ILIKE ${idx} OR owner_name ILIKE ${idx} OR phone ILIKE ${idx})"
            )
            params.append(f"%{search}%")
            idx += 1

        params.append(limit)
        where = " AND ".join(conditions)
        rows = await conn.fetch(
            f"""
            SELECT p.*,
                (SELECT direction FROM prospect_interactions
                 WHERE prospect_id = p.id ORDER BY created_at DESC LIMIT 1) AS last_message_direction,
                (SELECT content   FROM prospect_interactions
                 WHERE prospect_id = p.id ORDER BY created_at DESC LIMIT 1) AS last_message_preview
            FROM prospects p
            WHERE {where} ORDER BY p.updated_at DESC LIMIT ${idx}
            """,
            *params,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_create_prospect(
    restaurant_name: str,
    owner_name: str = "",
    phone: str = "",
    city: str = "",
    neighborhood: str = "",
    category: str = "",
    instagram: str = "",
    google_maps: str = "",
    source: str = "manual",
    stage: str = "prospecto",
    priority: str = "medium",
    revenue_est: int = 0,
    tags: List[str] = None,
) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO prospects
              (restaurant_name, owner_name, phone, city, neighborhood, category,
               instagram, google_maps, source, stage, priority, revenue_est, tags)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            RETURNING *
            """,
            restaurant_name, owner_name, phone, city, neighborhood, category,
            instagram, google_maps, source, stage, priority, revenue_est,
            tags or [],
        )
        return _serialize(dict(row))


async def db_get_prospects_last_updated() -> Optional[str]:
    """Return ISO timestamp of the most recently updated prospect, or None."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        latest = await conn.fetchval("SELECT MAX(updated_at) FROM prospects")
        if latest:
            return latest.isoformat()[:19] + "Z"
        return None


async def db_update_prospect(pid: int, updates: dict) -> Optional[dict]:
    """Update arbitrary columns on a prospect. Returns updated row or None if not found."""
    updates = dict(updates)
    updates["updated_at"] = datetime.utcnow()

    set_clauses = [f"{k} = ${i + 2}" for i, k in enumerate(updates.keys())]
    values = [pid] + list(updates.values())

    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE prospects SET {', '.join(set_clauses)} WHERE id=$1 RETURNING *",
            *values,
        )
        return _serialize(dict(row)) if row else None


async def db_delete_prospect(pid: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM prospects WHERE id=$1", pid)


async def db_move_prospect_stage(pid: int, new_stage: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE prospects SET stage=$2, updated_at=NOW() WHERE id=$1",
            pid, new_stage,
        )


# ── NOTES ─────────────────────────────────────────────────────────────────────

async def db_get_prospect_notes(pid: int) -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM prospect_notes WHERE prospect_id=$1 ORDER BY created_at DESC",
            pid,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_create_prospect_note(
    pid: int,
    author: str,
    content: str,
    note_type: str = "note",
) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO prospect_notes (prospect_id, author, content, note_type)
            VALUES ($1,$2,$3,$4) RETURNING *
            """,
            pid, author, content, note_type,
        )
        await conn.execute(
            "UPDATE prospects SET last_contact_at=NOW(), updated_at=NOW() WHERE id=$1",
            pid,
        )
        return _serialize(dict(row))


async def db_delete_prospect_note(nid: int, pid: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM prospect_notes WHERE id=$1 AND prospect_id=$2",
            nid, pid,
        )


# ── INTERACTIONS ──────────────────────────────────────────────────────────────

async def db_get_prospect_interactions(pid: int) -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM prospect_interactions WHERE prospect_id=$1 ORDER BY created_at ASC",
            pid,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_get_prospect_by_id(pid: int) -> Optional[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM prospects WHERE id=$1", pid)
        return _serialize(dict(row)) if row else None


async def db_get_prospect_by_phone(phone: str) -> Optional[dict]:
    """Return the latest prospect with this phone, or None."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, stage FROM prospects WHERE phone = $1 ORDER BY id DESC LIMIT 1",
            phone,
        )
        return dict(row) if row else None


async def db_record_outbound_interaction(
    pid: int,
    content: str,
    template_name: str = "",
    wa_message_id: str = "",
    status: str = "sent",
) -> None:
    """Record an outbound WhatsApp interaction (manual message or template)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if template_name:
            await conn.execute(
                """
                INSERT INTO prospect_interactions
                  (prospect_id, direction, channel, content, template_name, status, wa_message_id)
                VALUES ($1,'outbound','whatsapp',$2,$3,$4,$5)
                """,
                pid, content, template_name, status, wa_message_id,
            )
            await conn.execute(
                """
                UPDATE prospects
                SET last_contact_at=NOW(), updated_at=NOW(),
                    stage = CASE WHEN stage='prospecto' THEN 'contactado' ELSE stage END
                WHERE id=$1
                """,
                pid,
            )
        else:
            await conn.execute(
                """
                INSERT INTO prospect_interactions
                  (prospect_id, direction, channel, content, status, wa_message_id)
                VALUES ($1,'outbound','whatsapp',$2,$3,$4)
                """,
                pid, content, status, wa_message_id,
            )
            await conn.execute(
                "UPDATE prospects SET last_contact_at=NOW(), updated_at=NOW() WHERE id=$1",
                pid,
            )


async def db_record_inbound_interaction(
    phone: str,
    message: str,
    wa_message_id: str = "",
) -> None:
    """
    Register an inbound WhatsApp message from a prospect.
    Creates the prospect if not found. Called from chat.py CRM hook.
    """
    try:
        clean = phone.lstrip("+").replace(" ", "")
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, stage FROM prospects WHERE phone = $1 ORDER BY id DESC LIMIT 1",
                clean,
            )
            if not row:
                log.info("crm.prospect_creating_inbound", phone_suffix=clean[-4:])
                row = await conn.fetchrow(
                    """
                    INSERT INTO prospects (restaurant_name, phone, source, stage)
                    VALUES ($1, $2, 'inbound_whatsapp', 'respondio')
                    RETURNING id, stage
                    """,
                    f"Nuevo Inbound (+{clean[-4:]})", clean,
                )

            pid = row["id"]
            stage = row["stage"]

            await conn.execute(
                """
                INSERT INTO prospect_interactions
                  (prospect_id, direction, channel, content, status, wa_message_id)
                VALUES ($1,'inbound','whatsapp',$2,'received',$3)
                """,
                pid, message, wa_message_id,
            )

            new_stage = (
                "respondio"
                if stage in ("prospecto", "contactado", "perdido", "cerrado")
                else stage
            )
            await conn.execute(
                """
                UPDATE prospects
                SET last_contact_at=NOW(), updated_at=NOW(), stage=$2, archived=FALSE
                WHERE id=$1
                """,
                pid, new_stage,
            )
            log.info("crm.inbound_message_saved", prospect_id=pid, new_stage=new_stage)
    except Exception:
        # Do NOT re-raise: the WhatsApp webhook must not fail due to CRM errors.
        # log.exception captures the full stacktrace for debugging lost prospects.
        log.exception("crm.inbound_hook_failed", phone_suffix=phone[-4:] if phone else "?", wa_message_id=wa_message_id)
        log.error("crm.inbound_lost", phone_suffix=phone[-4:] if phone else "?", message_preview=message[:100] if message else "")


# ── TEMPLATES ─────────────────────────────────────────────────────────────────

async def db_get_crm_templates() -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM crm_templates ORDER BY id")
        return [_serialize(dict(r)) for r in rows]


async def db_get_crm_template_by_id(tid: int) -> Optional[dict]:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM crm_templates WHERE id=$1", tid)
        return _serialize(dict(row)) if row else None


async def db_create_crm_template(
    name: str,
    wa_name: str,
    language: str,
    category: str,
    body: str,
    params: List[str],
) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO crm_templates (name, wa_name, language, category, body, params)
            VALUES ($1,$2,$3,$4,$5,$6::TEXT[]) RETURNING *
            """,
            name, wa_name, language, category, body, params,
        )
        return _serialize(dict(row))


async def db_delete_crm_template(tid: int) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM crm_templates WHERE id=$1", tid)


# ── CSV UPSERT ────────────────────────────────────────────────────────────────

async def db_upsert_prospect_from_csv(
    name: str,
    owner: str,
    phone: str,
    city: str,
    neighborhood: str,
    category: str,
    instagram: str,
    google_maps: str,
    source: str,
    stage: str,
    priority: str,
) -> bool:
    """
    Insert prospect if phone does not exist. Returns True if inserted, False if skipped.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM prospects WHERE phone = $1", phone
        )
        if not existing:
            await conn.execute(
                """
                INSERT INTO prospects
                  (restaurant_name, owner_name, phone, city, neighborhood,
                   category, instagram, google_maps, source, stage, priority)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                """,
                name, owner, phone, city, neighborhood,
                category, instagram, google_maps, source, stage, priority,
            )
            return True
        return False


# ── STATS ─────────────────────────────────────────────────────────────────────

async def db_get_crm_stats() -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT stage, COUNT(*) as cnt FROM prospects WHERE archived=FALSE GROUP BY stage"
        )
        stage_counts = {r["stage"]: r["cnt"] for r in rows}

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM prospects WHERE archived=FALSE"
        )
        contacted = await conn.fetchval(
            """
            SELECT COUNT(DISTINCT p.id)
            FROM prospects p
            JOIN prospect_interactions pi ON p.id = pi.prospect_id
            WHERE p.archived=FALSE
              AND pi.direction='outbound'
              AND pi.status='sent'
            """
        )
        follow_ups = await conn.fetchval(
            """
            SELECT COUNT(*) FROM prospects
            WHERE next_follow_up <= NOW() + INTERVAL '24 hours'
            AND next_follow_up >= NOW()
            AND archived=FALSE
            """
        )

    converted = stage_counts.get("cerrado", 0)
    return {
        "stage_counts": stage_counts,
        "total": total or 0,
        "contacted": contacted or 0,
        "converted": converted,
        "follow_ups": follow_ups or 0,
        "conversion_rate": round((converted / total * 100) if total else 0, 1),
    }
