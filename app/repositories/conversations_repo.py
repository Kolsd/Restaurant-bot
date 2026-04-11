"""
Conversations repository — Fase 6 extraction from app.services.database.

Covers the conversations aggregate:
  - conversation history (get/save)
  - conversation listing, details, deletion
  - bot pause/unpause (toggle_bot, cleanup_old_conversations)
  - per-conversation NPS state (save_nps_response, pending score, waiting state)
  - cart CRUD (get, save, clear, migrate)
  - WAM deduplication (db_is_duplicate_wam)

Analytics-level NPS functions (db_get_nps_stats, db_get_nps_responses) remain
in app.services.database — they are restaurant-wide aggregates, not conversation state.

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.
"""

from __future__ import annotations

import json
from datetime import timedelta


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


def _to_date(s: str):
    from datetime import datetime  # local import — avoid module-level cost
    return datetime.strptime(s, "%Y-%m-%d").date()


# ── CONVERSACIONES ────────────────────────────────────────────────────

async def db_get_history(phone: str, bot_number: str = "") -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT history FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
        if row:
            h = row["history"]
            return h if isinstance(h, list) else json.loads(h)
        return []

async def db_save_history(phone: str, bot_number: str, history: list, branch_id: int = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 🛡️ Agregamos branch_id al INSERT y al UPDATE
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, history, branch_id, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (phone, bot_number)
            DO UPDATE SET history=EXCLUDED.history, branch_id=EXCLUDED.branch_id, updated_at=NOW()
        """, phone, bot_number, json.dumps(history[-20:]), branch_id)

async def db_get_all_conversations(bot_number: str = None, branch_id: int | str = None, date_from: str = None, date_to: str = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1

        if bot_number:
            conditions.append(f"bot_number = ${idx}")
            params.append(bot_number)
            idx += 1

        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass # Sin filtro, trae las conversaciones de toda la franquicia
        elif branch_id is not None:
            conditions.append(f"branch_id = ${idx}")
            params.append(branch_id)
            idx += 1
        elif bot_number:
            conditions.append("branch_id IS NULL")

        if date_from:
            conditions.append(f"created_at >= ${idx}")
            params.append(_to_date(date_from))
            idx += 1

        if date_to:
            conditions.append(f"created_at < ${idx}")
            params.append(_to_date(date_to) + timedelta(days=1))
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT phone, bot_number, history, updated_at, created_at FROM conversations {where} ORDER BY updated_at DESC"

        rows = await conn.fetch(query, *params)

        result = []
        for r in rows:
            history = r["history"] if isinstance(r["history"], list) else json.loads(r["history"])
            last_user = next(
                (m["content"] for m in reversed(history)
                 if m["role"] == "user" and isinstance(m.get("content"), str)),
                ""
            )
            has_voucher = any(
                "/api/media/" in (m.get("content") or "")
                for m in history if isinstance(m.get("content"), str)
            )
            result.append({
                "phone": r["phone"],
                "messages": len(history),
                "preview": last_user[:60] if last_user else "...",
                "updated_at": r["updated_at"].isoformat()[:19],
                "has_voucher": has_voucher,
            })
        return result

async def db_delete_conversation(phone: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)

async def db_get_conversation_details(phone: str, bot_number: str = ""):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history, bot_paused FROM conversations WHERE phone=$1 AND bot_number=$2",
            phone, bot_number,
        )
        if not row:
            # Fallback: conversaciones asignadas a sucursal tienen bot_number diferente
            row = await conn.fetchrow(
                "SELECT history, bot_paused FROM conversations WHERE phone=$1 ORDER BY updated_at DESC LIMIT 1",
                phone,
            )
        if row:
            history = row["history"] if isinstance(row["history"], list) else json.loads(row["history"])
            return {"history": history, "bot_paused": row["bot_paused"] or False}
    return {"history": [], "bot_paused": False}

async def db_toggle_bot(phone: str, bot_number: str, pause: bool):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, bot_paused, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET bot_paused=EXCLUDED.bot_paused, updated_at=NOW()
        """, phone, bot_number, pause)

async def db_cleanup_old_conversations(days: int = 7, bot_number: str = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL AND bot_number=$2", str(days), bot_number)
        else:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL", str(days))


# ── NPS — per-conversation state ──────────────────────────────────────
# (Restaurant-wide analytics db_get_nps_stats/db_get_nps_responses stay in database.py)

async def db_save_nps_response(phone: str, bot_number: str, score: int, comment: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 1. Inferimos el branch_id buscando la mesa donde acaba de comer el cliente
        branch_id = await conn.fetchval("""
            SELECT rt.branch_id FROM table_sessions ts
            JOIN restaurant_tables rt ON ts.table_id = rt.id
            WHERE ts.phone = $1 ORDER BY ts.started_at DESC LIMIT 1
        """, phone)

        # 2. Guardamos la calificación amarrada a esa sucursal
        await conn.execute("""
            INSERT INTO nps_responses (phone, bot_number, score, comment, branch_id, created_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
        """, phone, bot_number, score, comment, branch_id)


async def db_save_nps_pending(phone: str, bot_number: str, score: int) -> int:
    """Save a preliminary NPS record when score is received but comment is still pending.
    Returns the inserted row id so it can be updated later."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO nps_responses (phone, bot_number, score, comment)
               VALUES ($1, $2, $3, '__pending__') RETURNING id""",
            phone, bot_number, score
        )
        return row["id"] if row else 0


async def db_update_nps_comment(phone: str, bot_number: str, comment: str) -> bool:
    """Update the pending NPS record with the actual comment."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE nps_responses SET comment=$3
               WHERE phone=$1 AND bot_number=$2 AND comment='__pending__'
               AND created_at > NOW() - INTERVAL '24 hours'""",
            phone, bot_number, comment
        )
        return result != "UPDATE 0"


async def db_get_pending_nps_score(phone: str, bot_number: str) -> int | None:
    """Check if there is a pending NPS comment request in the DB (score saved, comment missing)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT score FROM nps_responses
               WHERE phone=$1 AND bot_number=$2 AND comment='__pending__'
               AND created_at > NOW() - INTERVAL '24 hours'
               ORDER BY created_at DESC LIMIT 1""",
            phone, bot_number
        )
        return row["score"] if row else None


# ── NPS WAITING STATE (persiste el estado "waiting_score" en DB) ──────

async def db_save_nps_waiting(phone: str, bot_number: str):
    """Persists that we are waiting for an NPS score from this customer.
    Called when trigger_nps is invoked so state survives server restarts."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nps_waiting (phone, bot_number, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET created_at = NOW()
        """, phone, bot_number)


async def db_get_nps_waiting(phone: str, bot_number: str) -> bool:
    """Returns True if there is a pending NPS score request for this customer (within 48 hours)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM nps_waiting WHERE phone=$1 AND bot_number=$2 AND created_at > NOW() - INTERVAL '48 hours'",
            phone, bot_number
        )
        return row is not None


async def db_clear_nps_waiting(phone: str, bot_number: str):
    """Removes the pending NPS state — called after score is received or survey is skipped."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM nps_waiting WHERE phone=$1 AND bot_number=$2",
            phone, bot_number
        )
        # Prune expired records while we're at it
        await conn.execute(
            "DELETE FROM nps_waiting WHERE created_at < NOW() - INTERVAL '48 hours'"
        )


# ── CARRITOS ─────────────────────────────────────────────────────────

async def db_get_cart(phone: str, bot_number: str) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT cart_data FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number)
        if row:
            return json.loads(row["cart_data"]) if isinstance(row["cart_data"], str) else row["cart_data"]
        return {"items": [], "order_type": None, "address": None, "notes": ""}

async def db_save_cart(phone: str, bot_number: str, cart_data: dict):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO carts (phone, bot_number, cart_data, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data=EXCLUDED.cart_data, updated_at=NOW()
        """, phone, bot_number, json.dumps(cart_data))

async def db_clear_cart(phone: str, bot_number: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number)

# 🛡️ NUEVO: Migrar el carrito atómicamente a otra sucursal
async def db_migrate_cart(phone: str, from_bot_number: str, to_bot_number: str):
    if from_bot_number == to_bot_number:
        return
    pool = await _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT cart_data FROM carts WHERE phone=$1 AND bot_number=$2", phone, from_bot_number)
            if row:
                await conn.execute("""
                    INSERT INTO carts (phone, bot_number, cart_data, updated_at)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data=EXCLUDED.cart_data, updated_at=NOW()
                """, phone, to_bot_number, row["cart_data"])
                await conn.execute("DELETE FROM carts WHERE phone=$1 AND bot_number=$2", phone, from_bot_number)


# ── WAM DEDUPLICATION ─────────────────────────────────────────────────

async def db_is_duplicate_wam(wam_id: str) -> bool:
    """
    Deduplicación idempotente por WAM_ID (WhatsApp Message ID).

    Lógica:
    - Borra entradas con más de 2 minutos (ventana de reintentos de Meta).
    - Intenta insertar el wam_id con INSERT ... ON CONFLICT DO NOTHING.
    - Si el INSERT no devuelve filas → el ID ya existía → duplicado (True).
    - Si el INSERT devuelve el wam_id → era nuevo → procesar (False).

    Al usar la PK como única constraint, el INSERT es atómico y safe
    bajo concurrencia multi-worker sin necesidad de locks adicionales.
    """
    if not wam_id:
        return False
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM processed_wam_ids WHERE received_at < NOW() - INTERVAL '2 minutes'"
        )
        result = await conn.fetchval(
            "INSERT INTO processed_wam_ids (wam_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING wam_id",
            wam_id,
        )
        # result is None  → row already existed → duplicate
        # result is wam_id → freshly inserted   → new message
        return result is None
