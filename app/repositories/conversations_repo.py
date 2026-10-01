"""
Conversations repository — Fase 6 extraction from app.services.database.

Covers the conversations aggregate:
  - conversation history (get/save)
  - conversation listing, details, deletion
  - bot pause/unpause (toggle_bot, cleanup_old_conversations)
  - per-conversation NPS state (save_nps_response, pending score, waiting state)
  - cart CRUD (get, save, clear)


Analytics-level NPS functions (db_get_nps_stats, db_get_nps_responses) remain
in app.services.database — they are restaurant-wide aggregates, not conversation state.

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


def _to_date(s: str):
    from datetime import datetime  # local import — avoid module-level cost
    return datetime.strptime(s, "%Y-%m-%d").date()


# ── CONVERSACIONES ────────────────────────────────────────────────────

async def db_get_history(phone: str, org_id: int) -> list:
    async with _tenant_connection() as conn:
        row = await conn.fetchrow("SELECT history FROM conversations WHERE phone=$1 AND org_id=$2", phone, org_id)
        if row:
            h = row["history"]
            return h if isinstance(h, list) else json.loads(h)
        return []


async def db_get_conversation_location_id(phone: str, org_id: int) -> int | None:
    """Return the last known location_id for a conversation, or None if not yet resolved.

    Used by _resolve_location_id in agent.py to persist the resolved Location
    across conversation turns without requiring re-resolution each time.
    """
    async with _tenant_connection() as conn:
        return await conn.fetchval(
            "SELECT location_id FROM conversations WHERE phone=$1 AND org_id=$2",
            phone, org_id,
        )


async def db_save_history(
    phone: str,
    org_id: int,
    history: list,
    branch_id: int = None,
    location_id: int | None = None,
):
    async with _tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, org_id, history, branch_id, location_id,
                                       updated_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (phone, org_id)
            DO UPDATE SET history=EXCLUDED.history, branch_id=EXCLUDED.branch_id,
                          location_id=COALESCE(EXCLUDED.location_id, conversations.location_id),
                          updated_at=NOW()
        """, phone, org_id, json.dumps(history[-20:]), branch_id, location_id)

async def db_increment_turns_without_progress(phone: str, org_id: int) -> int:
    """Increment turns_without_progress counter and return the new value.

    The row is expected to already exist (chat() calls db_save_history before/after
    each turn). If the row is missing, this is a no-op and returns 0 (fail-open).
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE conversations
               SET turns_without_progress = turns_without_progress + 1
             WHERE phone=$1 AND org_id=$2
            RETURNING turns_without_progress
            """,
            phone, org_id,
        )
        return row["turns_without_progress"] if row else 0


async def db_reset_turns_without_progress(phone: str, org_id: int) -> None:
    """Reset turns_without_progress to 0 after a productive tool call."""
    async with _tenant_connection() as conn:
        await conn.execute(
            "UPDATE conversations SET turns_without_progress = 0 WHERE phone=$1 AND org_id=$2",
            phone, org_id,
        )


async def db_get_all_conversations(org_id: int | None = None, branch_id: int | str = None, date_from: str = None, date_to: str = None):
    async with _tenant_connection() as conn:
        conditions = []
        params = []
        idx = 1

        if org_id:
            conditions.append(f"org_id = ${idx}")
            params.append(org_id)
            idx += 1

        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass # Sin filtro, trae las conversaciones de toda la franquicia
        elif branch_id is not None:
            conditions.append(f"branch_id = ${idx}")
            params.append(branch_id)
            idx += 1
        elif org_id:
            pass  # org_id filter already applied above

        if date_from:
            conditions.append(f"created_at >= ${idx}")
            params.append(_to_date(date_from))
            idx += 1

        if date_to:
            conditions.append(f"created_at < ${idx}")
            params.append(_to_date(date_to) + timedelta(days=1))
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        # Hard cap: history JSONB can be 100s of KB per row. 50K conversations
        # without LIMIT = OOM risk + unresponsive admin page. The dashboard
        # only renders the most recent 200 anyway; admins paginate via
        # date_from/date_to for older windows.
        query = (
            f"SELECT phone, history, updated_at, created_at "
            f"FROM conversations {where} ORDER BY updated_at DESC LIMIT 200"
        )

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
    async with _tenant_connection() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)

async def db_get_conversation_details(phone: str, org_id: int):
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT history, bot_paused FROM conversations WHERE phone=$1 AND org_id=$2",
            phone, org_id,
        )
        if row:
            history = row["history"] if isinstance(row["history"], list) else json.loads(row["history"])
            return {"history": history, "bot_paused": row["bot_paused"] or False}
    return {"history": [], "bot_paused": False}


async def db_cleanup_old_conversations(days: int = 7, org_id: int | None = None):
    async with _tenant_connection() as conn:
        if org_id:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL AND org_id=$2", str(days), org_id)
        else:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL", str(days))


# ── NPS — per-conversation state ──────────────────────────────────────
# (Restaurant-wide analytics db_get_nps_stats/db_get_nps_responses stay in database.py)

async def db_save_nps_response(
    phone: str, org_id: int, score: int, comment: str,
    location_id: int | None = None,
):
    """`location_id` attributes a rating that has no table session behind it
    (a web delivery/pickup order knows its own sede). Without it such
    ratings were stored with no sede and vanished from per-sede NPS."""
    async with _tenant_connection() as conn:
        # 1. Resolve branch_id + table_session_id in ONE query. The session is the
        #    customer's last dine-in sitting — gives us both the sede and the FK
        #    used for per-mesero NPS aggregation (via table_sessions.assigned_staff_id).
        #    NULLs are fine for delivery/pickup NPS (no session).
        row = await conn.fetchrow("""
            SELECT ts.id AS session_id, rt.branch_id AS branch_id,
                   rt.location_id AS table_location_id
            FROM table_sessions ts
            JOIN restaurant_tables rt ON ts.table_id = rt.id
            WHERE ts.phone = $1 AND ts.started_at > NOW() - INTERVAL '24 hours'
            ORDER BY ts.started_at DESC LIMIT 1
        """, phone)
        session_id = row["session_id"] if row else None
        branch_id  = row["branch_id"]  if row else None
        # The sede the per-sede NPS reads. A table answer used to leave it
        # NULL, so "NPS por sede" was always empty for dine-in.
        if location_id is None and row is not None:
            location_id = row["table_location_id"]
        if location_id is not None:
            # The caller knows exactly which sede served this order — that
            # beats inferring it from a table session (there is none).
            branch_id = location_id

        # 2. Save the rating tied to that branch + session
        await conn.execute("""
            INSERT INTO nps_responses
                (phone, org_id, score, comment, branch_id, table_session_id,
                 location_id, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
        """, phone, org_id, score, comment, branch_id, session_id, location_id)


async def db_save_nps_pending(phone: str, org_id: int, score: int) -> int:
    """Save a preliminary NPS record when score is received but comment is still pending.
    Returns the inserted row id so it can be updated later.

    Also captures table_session_id for per-waiter NPS aggregation — the session may
    have closed between trigger_nps and the score arriving, but the last active one
    is our best attribution hint.
    """
    async with _tenant_connection() as conn:
        # Capture the session + branch at score time (not at comment time) — the
        # session is the customer's last sitting, which is what the NPS refers to.
        attrib = await conn.fetchrow("""
            SELECT ts.id AS session_id, rt.branch_id AS branch_id,
                   rt.location_id AS table_location_id
            FROM table_sessions ts
            JOIN restaurant_tables rt ON ts.table_id = rt.id
            WHERE ts.phone = $1 AND ts.started_at > NOW() - INTERVAL '24 hours'
            ORDER BY ts.started_at DESC LIMIT 1
        """, phone)
        session_id = attrib["session_id"] if attrib else None
        branch_id  = attrib["branch_id"]  if attrib else None
        location_id = attrib["table_location_id"] if attrib else None

        row = await conn.fetchrow(
            """INSERT INTO nps_responses
                   (phone, org_id, score, comment, branch_id, table_session_id, location_id)
               VALUES ($1, $2, $3, '__pending__', $4, $5, $6)
               RETURNING id""",
            phone, org_id, score, branch_id, session_id, location_id
        )
        return row["id"] if row else 0


async def db_update_nps_comment(phone: str, org_id: int, comment: str) -> bool:
    """Update the pending NPS record with the actual comment."""
    async with _tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE nps_responses SET comment=$3
               WHERE phone=$1 AND org_id=$2 AND comment='__pending__'
               AND created_at > NOW() - INTERVAL '24 hours'""",
            phone, org_id, comment
        )
        return result != "UPDATE 0"


# ── NPS WAITING STATE (persists the "waiting_score" state in DB) ──────

async def db_save_nps_waiting(phone: str, org_id: int):
    """Persists that we are waiting for an NPS score from this customer.
    Called when trigger_nps is invoked so state survives server restarts."""
    async with _tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO nps_waiting (phone, org_id, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (phone, org_id) DO UPDATE SET created_at = NOW()
        """, phone, org_id)


async def db_clear_nps_waiting(phone: str, org_id: int):
    """Removes the pending NPS state — called after score is received or survey is skipped."""
    async with _tenant_connection() as conn:
        await conn.execute(
            "DELETE FROM nps_waiting WHERE phone=$1 AND org_id=$2",
            phone, org_id
        )
        # Prune expired records while we're at it
        await conn.execute(
            "DELETE FROM nps_waiting WHERE created_at < NOW() - INTERVAL '48 hours'"
        )


async def db_cleanup_expired_nps_waiting() -> int:
    """Delete nps_waiting rows older than 48h. Returns deleted row count.

    Runs as a scheduler cleanup phase. Removes rows whose 48h window has
    expired regardless of state (replied, reminded, or ignored).
    """
    async with _tenant_connection() as conn:
        result = await conn.execute(
            "DELETE FROM nps_waiting WHERE created_at < NOW() - INTERVAL '48 hours'"
        )
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


# ── CARRITOS ─────────────────────────────────────────────────────────

async def db_get_cart(phone: str, org_id: int) -> dict:
    async with _tenant_connection() as conn:
        row = await conn.fetchrow("SELECT cart_data FROM carts WHERE phone=$1 AND org_id=$2", phone, org_id)
        if row:
            return json.loads(row["cart_data"]) if isinstance(row["cart_data"], str) else row["cart_data"]
        return {"items": [], "order_type": None, "address": None, "notes": ""}

async def db_save_cart(phone: str, org_id: int, cart_data: dict):
    async with _tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO carts (phone, org_id, cart_data, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            ON CONFLICT (phone, org_id) DO UPDATE SET cart_data=EXCLUDED.cart_data, updated_at=NOW()
        """, phone, org_id, json.dumps(cart_data))

async def db_clear_cart(phone: str, org_id: int):
    async with _tenant_connection() as conn:
        await conn.execute("DELETE FROM carts WHERE phone=$1 AND org_id=$2", phone, org_id)


# ── RATE LIMITING ─────────────────────────────────────────────────────


# ── RESTAURANT WA_PHONE_ID AUTO-PERSIST ───────────────────────────────


async def db_update_restaurant_features(restaurant_id: int, features: dict) -> None:
    """Overwrite the features JSONB column for a restaurant row.

    Post-Wave-2: `restaurants` is a READ-ONLY VIEW. Write to `organizations` instead.
    `restaurant_id` is org_id.
    """
    import json as _json
    async with _tenant_connection() as conn:
        await conn.execute(
            "UPDATE organizations SET features = $1::jsonb WHERE id = $2",
            _json.dumps(features), restaurant_id,
        )
