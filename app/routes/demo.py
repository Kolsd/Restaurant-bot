"""
app/routes/demo.py — /api/demo/* endpoints for the live chat widget on /demo.

Architecture summary:
  - POST /api/demo/start   → create a session (Redis TTL 1h), return session_id
  - POST /api/demo/chat    → validate session, call agent.chat(), return reply
  - POST /api/demo/reset   → delete session + clean up DB conversation/cart

Security:
  - Demo sessions are sandboxed to the Demo Mesio org (org_id from demo_repo).
  - Each session uses a synthetic customer_phone ("demo:<uuid>") that never
    touches real WhatsApp or Meta APIs.
  - Rate limit: 100 session starts per IP per hour (Redis-backed).
  - 30-message cap per session to prevent LLM cost abuse.
  - tenant_scope(demo_org_id) is set before calling agent.chat() (Rule 14).
  - XSS: user input is never reflected in HTML — all output goes through JSON.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import state_store
from app.services.logging import get_logger
from app.services.tenant_context import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/api/demo", tags=["demo"])

# ── Constants ─────────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS = 3600          # 1 hour
MAX_MESSAGES_PER_SESSION = 30
RATE_LIMIT_STARTS_PER_HOUR = 100   # per IP

DEMO_BOT_NUMBER = "demo:0"         # synthetic — never hits Meta
DEMO_PHONE_ID = ""                  # agent.chat handles empty string gracefully
CAP_REPLY = (
    "Esta sesión de demo está limitada a 30 mensajes. "
    "Pulsa Reiniciar para comenzar una sesión nueva."
)

# ── Redis key helpers ─────────────────────────────────────────────────────────

def _session_key(session_id: str) -> str:
    return f"mesio:demo:session:{session_id}"


def _ip_ratelimit_key(ip: str) -> str:
    return f"demo_start:{ip}"


# ── Session helpers ───────────────────────────────────────────────────────────

async def _get_session(session_id: str) -> dict | None:
    """Return the session dict or None if missing / expired."""
    from app.services import redis_client as _rc  # noqa: PLC0415
    r = await _rc.get_redis()
    if r is not None:
        raw = await r.get(_session_key(session_id))
        if raw is None:
            return None
        return json.loads(raw)
    # Fallback: state_store generic — use rate_limit_check infrastructure but
    # for session storage we need a proper get; use the checkout family as a
    # structural template via direct fallback access.
    # When Redis is unavailable we can't reliably serve demo sessions across
    # multiple workers, so we return None (session not found) and let the
    # client call /start again to get a fresh one.
    log.warning("demo.session_lookup.redis_unavailable", session_id=session_id[:8])
    return None


async def _save_session(session_id: str, data: dict) -> None:
    from app.services import redis_client as _rc  # noqa: PLC0415
    r = await _rc.get_redis()
    if r is not None:
        await r.set(
            _session_key(session_id),
            json.dumps(data),
            ex=SESSION_TTL_SECONDS,
        )


async def _delete_session(session_id: str) -> None:
    from app.services import redis_client as _rc  # noqa: PLC0415
    r = await _rc.get_redis()
    if r is not None:
        await r.delete(_session_key(session_id))


async def _increment_message_count(session_id: str, data: dict) -> int:
    """Increment message_count in the session and persist. Returns new count."""
    data["message_count"] = data.get("message_count", 0) + 1
    await _save_session(session_id, data)
    return data["message_count"]


# ── Pydantic request models ───────────────────────────────────────────────────

class StartRequest(BaseModel):
    pass  # no body required


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ResetRequest(BaseModel):
    session_id: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/start")
async def demo_start(request: Request, _body: StartRequest = StartRequest()):
    """Create a fresh demo session. Rate-limited per IP."""
    # Rate limit: 100 starts/hour per IP
    ip = request.client.host if request.client else "unknown"
    allowed = await state_store.rate_limit_check(
        key=_ip_ratelimit_key(ip),
        max_requests=RATE_LIMIT_STARTS_PER_HOUR,
        window_seconds=3600,
    )
    if not allowed:
        log.warning("demo.start.rate_limited", ip=ip)
        raise HTTPException(
            status_code=429,
            detail="Demasiadas sesiones iniciadas. Intenta más tarde.",
        )

    # Resolve demo org
    from app.repositories.demo_repo import db_get_demo_org_id  # noqa: PLC0415
    org_id = await db_get_demo_org_id()
    if org_id is None:
        raise HTTPException(
            status_code=503,
            detail="Demo no disponible en este momento. Contacta a soporte.",
        )

    session_id = str(uuid.uuid4())
    customer_phone = f"demo:{session_id}"

    session_data = {
        "customer_phone": customer_phone,
        "restaurant_id": org_id,
        "message_count": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await _save_session(session_id, session_data)

    log.info("demo.session_started", session_id=session_id[:8], org_id=org_id)

    return {
        "session_id": session_id,
        "customer_phone": customer_phone,
        "restaurant_id": org_id,
        "restaurant_name": "Demo Mesio",
    }


@router.post("/chat")
async def demo_chat(body: ChatRequest):
    """Send a message to the demo bot and get a reply."""
    session = await _get_session(body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o expirada.")

    current_count = session.get("message_count", 0)

    # Hard cap at 30 messages — return expiry notice without calling LLM
    if current_count >= MAX_MESSAGES_PER_SESSION:
        return {
            "reply": CAP_REPLY,
            "message_count": current_count,
            "max_messages": MAX_MESSAGES_PER_SESSION,
            "expired": True,
        }

    org_id: int = session["restaurant_id"]
    customer_phone: str = session["customer_phone"]
    user_message: str = body.message.strip()

    if not user_message:
        return {
            "reply": "",
            "message_count": current_count,
            "max_messages": MAX_MESSAGES_PER_SESSION,
            "expired": False,
        }

    # Increment BEFORE calling the bot to prevent race-condition double-sends
    new_count = await _increment_message_count(body.session_id, session)

    # Call the bot inside correct tenant scope (Rule 14)
    from app.services.agent import chat as agent_chat  # noqa: PLC0415

    reply_text = ""
    try:
        with tenant_scope(org_id):
            result = await agent_chat(
                user_phone=customer_phone,
                user_message=user_message,
                bot_number=DEMO_BOT_NUMBER,
                meta_phone_id=DEMO_PHONE_ID,
                location_id=None,
            )
        # agent.chat() returns dict with "message" key, or None
        if result and isinstance(result, dict):
            reply_text = result.get("message", "")
        elif result is None:
            reply_text = ""
    except Exception:
        log.exception(
            "demo.chat.agent_error",
            session_id=body.session_id[:8],
            org_id=org_id,
        )
        reply_text = "Hubo un problema técnico. Por favor intenta de nuevo."

    return {
        "reply": reply_text,
        "message_count": new_count,
        "max_messages": MAX_MESSAGES_PER_SESSION,
        "expired": False,
    }


@router.post("/reset")
async def demo_reset(body: ResetRequest):
    """Clear a demo session and its conversation/cart DB rows."""
    session = await _get_session(body.session_id)
    if session is None:
        # Already gone — idempotent
        return {"ok": True}

    org_id: int = session["restaurant_id"]
    customer_phone: str = session["customer_phone"]

    # Delete Redis session first so subsequent /chat calls on this session_id fail
    await _delete_session(body.session_id)

    # Clean up DB rows inside tenant scope
    from app.repositories.demo_repo import db_cleanup_demo_session  # noqa: PLC0415
    try:
        with tenant_scope(org_id):
            await db_cleanup_demo_session(customer_phone, org_id)
    except Exception:
        log.exception(
            "demo.reset.cleanup_error",
            session_id=body.session_id[:8],
            org_id=org_id,
        )
        # Non-fatal — the Redis session is already deleted; stale DB rows
        # accumulate only for demo org and can be pruned periodically.

    log.info("demo.session_reset", session_id=body.session_id[:8])
    return {"ok": True}
