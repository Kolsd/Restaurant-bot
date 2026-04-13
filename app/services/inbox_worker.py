"""
Inbox worker — polls webhook_inbox and dispatches to registered handlers.

Design:
- One asyncio loop per uvicorn worker process.
- FOR UPDATE SKIP LOCKED ensures multiple workers don't double-process rows.
- Graceful shutdown via asyncio.Event.
- Exponential backoff and dead-letter are handled by inbox_repo.mark_failed.
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
    p95_ms = (sorted(lats)[int(len(lats) * 0.95)] * 1000) if len(lats) >= 2 else avg_ms
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

    IMPORTANT: fetch, dispatch, and mark_processed/mark_failed must all happen
    within the SAME transaction on the SAME connection.  FOR UPDATE SKIP LOCKED
    only holds the row lock while the transaction is open — closing it early
    releases the lock and lets other workers grab the same row.
    """
    from app.services import database as db  # late import avoids circular

    log.info("inbox_worker_started")

    while not stop_event.is_set():
        pool = await db.get_pool()

        try:
            processed_count = 0

            # Process up to _BATCH_SIZE rows, one per transaction so a single
            # failure doesn't roll back the others.
            for _ in range(_BATCH_SIZE):
                if stop_event.is_set():
                    break

                async with pool.acquire() as conn:
                    async with conn.transaction():
                        # fetch_batch holds FOR UPDATE lock inside this transaction
                        rows = await inbox_repo.fetch_batch(conn, limit=1)
                        if not rows:
                            break  # no more pending rows

                        row      = rows[0]
                        inbox_id = row["id"]
                        provider = row["provider"]
                        payload  = row["payload"]
                        attempts = row["attempts"]

                        # dispatch and mark happen under the same lock
                        _t0 = _time.monotonic()
                        try:
                            await asyncio.wait_for(_dispatch(provider, payload), timeout=120)
                            await inbox_repo.mark_processed(conn, inbox_id)
                            _elapsed = _time.monotonic() - _t0
                            _latencies.append(_elapsed)
                            _metrics["processed"] += 1
                            log.info(
                                "inbox_processed",
                                inbox_id=inbox_id,
                                provider=provider,
                                latency_ms=round(_elapsed * 1000, 1),
                            )
                            processed_count += 1
                        except asyncio.TimeoutError:
                            _elapsed = _time.monotonic() - _t0
                            _latencies.append(_elapsed)
                            _metrics["errors"] += 1
                            error_str = "dispatch_timeout: handler exceeded 120s"
                            log.error(
                                "inbox_dispatch_timeout",
                                inbox_id=inbox_id,
                                provider=provider,
                                attempts=attempts + 1,
                                latency_ms=round(_elapsed * 1000, 1),
                                customer_phone=payload.get("user_phone", "unknown"),
                                bot_number=payload.get("bot_number", "unknown"),
                            )
                            await inbox_repo.mark_failed(
                                conn, inbox_id, error_str, attempts
                            )
                            processed_count += 1
                        except Exception as exc:
                            _elapsed = _time.monotonic() - _t0
                            _latencies.append(_elapsed)
                            _metrics["errors"] += 1
                            error_str = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                            log.error(
                                "inbox_dispatch_failed",
                                inbox_id=inbox_id,
                                provider=provider,
                                attempts=attempts + 1,
                                error=str(exc),
                                latency_ms=round(_elapsed * 1000, 1),
                                customer_phone=payload.get("user_phone", "unknown"),
                                bot_number=payload.get("bot_number", "unknown"),
                            )
                            await inbox_repo.mark_failed(
                                conn, inbox_id, error_str, attempts
                            )
                            processed_count += 1

            if processed_count == 0:
                # Nothing to do — wait before polling again
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=_POLL_INTERVAL_EMPTY
                    )
                except asyncio.TimeoutError:
                    pass
            else:
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
        user_phone, user_text, bot_number, phone_id, access_token
    """
    from app.routes.chat import _process_message

    await _process_message(
        user_phone   = payload["user_phone"],
        user_text    = payload["user_text"],
        bot_number   = payload["bot_number"],
        phone_id     = payload["phone_id"],
        access_token = payload["access_token"],
    )


# Register at import time so the worker is ready before any message arrives.
register_handler("meta_whatsapp", _handle_meta_whatsapp)
