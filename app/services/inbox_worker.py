"""
Inbox worker — polls webhook_inbox and dispatches to registered handlers.

Design:
- One asyncio loop per uvicorn worker process.
- FOR UPDATE SKIP LOCKED ensures multiple workers don't double-process rows.
- Graceful shutdown via asyncio.Event.
- Exponential backoff and dead-letter are handled by inbox_repo.mark_failed.

Claim-then-ack pattern (three phases):
  Phase 1 — short transaction (~ms):
    Acquire conn → BEGIN → fetch_batch (FOR UPDATE SKIP LOCKED) →
    claim_rows (next_attempt_at = NOW() + 3 min, attempts += 1) →
    COMMIT → release conn.
  Phase 2 — no DB connection held (up to 120 s):
    dispatch via asyncio.wait_for(timeout=120).
  Phase 3 — short transaction (~ms):
    Acquire NEW conn → mark_processed or mark_failed → COMMIT → release conn.

This eliminates three structural bugs in the original design:
  1. Connection pool starvation: connections were held for 120 s during dispatch,
     blocking every other query (including chat() itself).
  2. mark_failed inside transaction: on DB error inside the same transaction the
     attempt counter rolled back → infinite retry with no backoff.
  3. mark_processed inside transaction: on DB error the row was never marked done
     → customer received duplicate responses on the re-dispatch.

Crash safety: if the worker dies between Phase 1 and Phase 3, the claimed row
becomes visible again after the 3-minute window and is retried automatically.
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Awaitable, Callable

import time as _time
from collections import deque

from app.services.logging import get_logger
from app.repositories import inbox_repo

log = get_logger(__name__)

# Handler registry: provider string → async callable(payload: dict) -> None
_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

# ── Processing metrics (read by /health/metrics) ─────────────────────────────
_metrics = {
    "processed": 0,
    "errors": 0,
}
_latencies: deque[float] = deque(maxlen=100)  # rolling window of last 100


def get_metrics() -> dict:
    """Return a snapshot of worker processing metrics."""
    lats = list(_latencies)
    avg_ms = (sum(lats) / len(lats) * 1000) if lats else 0.0
    idx    = min(int(len(lats) * 0.95), len(lats) - 1) if lats else 0
    p95_ms = sorted(lats)[idx] * 1000 if lats else avg_ms
    return {
        "inbox_processed_total": _metrics["processed"],
        "inbox_errors_total": _metrics["errors"],
        "inbox_latency_avg_ms": round(avg_ms, 1),
        "inbox_latency_p95_ms": round(p95_ms, 1),
        "inbox_latency_samples": len(lats),
    }

_POLL_INTERVAL_EMPTY = 1.0   # seconds to sleep when batch was empty
_BATCH_SIZE          = 10


def register_handler(provider: str, fn: Callable[[dict], Awaitable[None]]) -> None:
    """Register an async handler for a given provider string."""
    _handlers[provider] = fn
    log.info("inbox_handler_registered", provider=provider)


async def _dispatch(provider: str, payload: dict) -> None:
    handler = _handlers.get(provider)
    if handler is None:
        raise ValueError(f"No handler registered for provider '{provider}'")
    await handler(payload)


async def run_worker(stop_event: asyncio.Event) -> None:
    """
    Main worker loop.  Runs until *stop_event* is set.
    Call this from the FastAPI lifespan startup as an asyncio.Task.

    Uses the claim-then-ack pattern — see module docstring for details.
    """
    from app.services import database as db  # late import avoids circular

    log.info("inbox_worker_started")

    while not stop_event.is_set():
        pool = await db.get_pool()

        try:
            processed_count = 0

            # ── Phase 1: claim a batch in a short transaction ─────────────────
            # fetch_batch uses FOR UPDATE SKIP LOCKED to atomically select rows
            # that no other worker has locked.  claim_rows immediately sets
            # next_attempt_at 3 minutes ahead so the rows are invisible to other
            # workers even after we release the connection.  The whole thing
            # commits in under a millisecond — no long-held connections.
            claimed: list[dict] = []
            async with pool.acquire() as conn:
                async with conn.transaction():
                    rows = await inbox_repo.fetch_batch(conn, limit=_BATCH_SIZE)
                    if rows:
                        await inbox_repo.claim_rows(
                            conn, [r["id"] for r in rows]
                        )
                        # Snapshot the fields we need; the connection is released
                        # after this block so we cannot touch these records again.
                        for r in rows:
                            claimed.append({
                                "id":       r["id"],
                                "provider": r["provider"],
                                "payload":  r["payload"],
                                # attempts as read from DB; claim_rows already
                                # incremented the DB value by 1.
                                "attempts": r["attempts"] + 1,
                            })
            # Connection fully released here — pool is free for chat() etc.

            if not claimed:
                # Nothing to do — wait before polling again
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=_POLL_INTERVAL_EMPTY
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            # ── Phase 2: dispatch (no DB connection held) ─────────────────────
            for item in claimed:
                if stop_event.is_set():
                    break

                inbox_id = item["id"]
                provider = item["provider"]
                payload  = item["payload"]
                attempts = item["attempts"]  # already incremented by Phase 1

                _t0 = _time.monotonic()
                dispatch_error: str | None = None

                try:
                    await asyncio.wait_for(_dispatch(provider, payload), timeout=120)
                    # success
                except asyncio.TimeoutError:
                    _metrics["errors"] += 1
                    dispatch_error = "dispatch_timeout: handler exceeded 120s"
                    log.error(
                        "inbox_dispatch_timeout",
                        inbox_id=inbox_id,
                        provider=provider,
                        attempts=attempts,
                        latency_ms=round((_time.monotonic() - _t0) * 1000, 1),
                        customer_phone=payload.get("user_phone", "unknown"),
                        bot_number=payload.get("bot_number", "unknown"),
                    )
                except Exception as exc:
                    _metrics["errors"] += 1
                    # Unregistered provider → dead-letter immediately, no retries
                    if isinstance(exc, ValueError) and "No handler registered" in str(exc):
                        dispatch_error = f"DEAD_LETTER: {exc}"
                    else:
                        dispatch_error = (
                            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                        )
                    log.error(
                        "inbox_dispatch_failed",
                        inbox_id=inbox_id,
                        provider=provider,
                        attempts=attempts,
                        error=str(exc),
                        latency_ms=round((_time.monotonic() - _t0) * 1000, 1),
                        customer_phone=payload.get("user_phone", "unknown"),
                        bot_number=payload.get("bot_number", "unknown"),
                    )

                _elapsed = _time.monotonic() - _t0
                _latencies.append(_elapsed)

                # ── Phase 3: ack in a new short transaction ───────────────────
                # A fresh connection is acquired here; no transaction has been
                # open since Phase 1 completed.  If Phase 3 itself fails (e.g.
                # transient DB error), the row stays claimed with
                # next_attempt_at = NOW() + 3 min and will be retried
                # automatically — no duplicate dispatch within that window.
                if dispatch_error is None:
                    try:
                        async with pool.acquire() as conn:
                            await inbox_repo.mark_processed(conn, inbox_id)
                        _metrics["processed"] += 1
                        log.info(
                            "inbox_processed",
                            inbox_id=inbox_id,
                            provider=provider,
                            latency_ms=round(_elapsed * 1000, 1),
                        )
                    except Exception:
                        # Row is claimed; won't be re-dispatched for 3 minutes.
                        # The next worker that picks it up will also call
                        # _dispatch and mark_processed — acceptable rare duplicate
                        # versus holding the connection for 120 s.
                        log.exception(
                            "inbox_mark_processed_failed",
                            inbox_id=inbox_id,
                            provider=provider,
                        )
                else:
                    try:
                        async with pool.acquire() as conn:
                            await inbox_repo.mark_failed(
                                conn,
                                inbox_id,
                                dispatch_error,
                                attempts,
                                already_incremented=True,
                            )
                    except Exception:
                        # Same crash-safety argument as above.
                        log.exception(
                            "inbox_mark_failed_failed",
                            inbox_id=inbox_id,
                            provider=provider,
                            dispatch_error=dispatch_error,
                        )

                processed_count += 1

            if processed_count > 0:
                # Had work — yield to event loop then poll immediately
                await asyncio.sleep(0)

        except Exception:
            log.exception("inbox_worker_poll_error")
            # Brief pause to avoid tight error loops on DB failures
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=5.0
                )
            except asyncio.TimeoutError:
                pass

    log.info("inbox_worker_stopped")


# ── Handler: meta_whatsapp ────────────────────────────────────────────────────

async def _handle_meta_whatsapp(payload: dict) -> None:
    """
    Reconstruct the arguments that routes/chat.py's _process_message expects
    and call it directly.  The payload stored in webhook_inbox is the dict that
    was already parsed + enriched by the webhook route before enqueuing.

    Expected keys (set by routes/chat.py before enqueue):
        user_phone, user_text, bot_number, phone_id

    For voice notes: user_text is "" and needs_transcription is True.
        audio_id is included; transcription happens here before _process_message.

    access_token is fetched from the DB at dispatch time so it is never
    stored in the inbox table (including dead-letter rows).
    """
    import os
    from app.routes.chat import _process_message, _send_wa_text
    from app.services import database as _db
    from app.services.transcription import (
        download_whatsapp_media,
        transcribe_audio,
        TranscriptionError,
        TranscriptionUnavailable,
        AudioTooLongError,
    )

    from app.services.tenant_context import tenant_scope  # noqa: PLC0415

    bot_number = payload["bot_number"]
    user_phone = payload["user_phone"]
    phone_id   = payload["phone_id"]

    # Fetch access_token (same lookup the handler already does for text messages).
    # Connection is acquired and released here, BEFORE any long I/O — Rule 4.
    restaurant = await _db.db_get_restaurant_by_phone(bot_number)
    if restaurant and restaurant.get("wa_access_token"):
        access_token = restaurant["wa_access_token"]
    else:
        access_token = os.getenv("META_ACCESS_TOKEN") or os.getenv("WHATSAPP_TOKEN", "")

    # Resolve tenant_id for downstream tenant-scoped repo calls.
    # If restaurant cannot be resolved, proceed without scope (legacy path).
    _tenant_id = restaurant["id"] if restaurant else None

    # ── Voice note: transcribe before calling _process_message ────────────────
    if payload.get("needs_transcription"):
        audio_id = payload.get("audio_id", "")

        if not access_token:
            log.error("audio.no_access_token", bot_number=bot_number, audio_id=audio_id)
            # Ack (do not raise) — retrying forever won't help without a token.
            return

        try:
            audio_bytes, mime_type = await download_whatsapp_media(audio_id, access_token)
            transcribed = await transcribe_audio(audio_bytes, mime_type=mime_type)
        except TranscriptionUnavailable:
            log.warning("audio.key_unavailable", audio_id=audio_id)
            await _send_wa_text(
                user_phone,
                "Aún no proceso audios aquí. ¿Me lo escribes? 🙂",
                phone_id,
                access_token,
            )
            return  # ack — do not retry, key won't appear on its own
        except AudioTooLongError:
            log.warning("audio.too_long", audio_id=audio_id)
            await _send_wa_text(
                user_phone,
                "El audio es muy largo. ¿Me mandas uno más corto o lo escribes?",
                phone_id,
                access_token,
            )
            return  # ack — retrying won't shrink the file
        except TranscriptionError:
            # Transient failure — re-raise so the inbox worker marks this row
            # as failed and retries with exponential backoff (Rule 13).
            log.exception("audio.transient_failure", audio_id=audio_id)
            raise
        except Exception:
            # Unknown failure — send fallback and ack (do not retry unknown errors).
            log.exception("audio.processing_failed", audio_id=audio_id)
            await _send_wa_text(
                user_phone,
                "No pude entender tu audio. ¿Me lo escribes?",
                phone_id,
                access_token,
            )
            return

        if not transcribed.strip():
            await _send_wa_text(
                user_phone,
                "No escuché nada en tu audio. ¿Intentas de nuevo o me escribes?",
                phone_id,
                access_token,
            )
            return  # ack — nothing to process

        user_text = transcribed
    else:
        user_text = payload.get("user_text", "")

    # Wrap _process_message in tenant_scope so that any tenant-scoped repo calls
    # downstream (agent.py → orders.py → customer_profiles_repo, etc.) have an
    # active tenant context.  If restaurant could not be resolved, skip the scope
    # and let the existing error handling in _process_message deal with it.
    if _tenant_id is not None:
        with tenant_scope(_tenant_id):
            await _process_message(
                user_phone   = user_phone,
                user_text    = user_text,
                bot_number   = bot_number,
                phone_id     = phone_id,
                access_token = access_token,
            )
    else:
        await _process_message(
            user_phone   = user_phone,
            user_text    = user_text,
            bot_number   = bot_number,
            phone_id     = phone_id,
            access_token = access_token,
        )


# Register at import time so the worker is ready before any message arrives.
register_handler("meta_whatsapp", _handle_meta_whatsapp)
assert "meta_whatsapp" in _handlers, "meta_whatsapp handler not registered"
