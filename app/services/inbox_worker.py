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

from app.services.logging import get_logger
from app.repositories import inbox_repo

log = get_logger(__name__)

# Handler registry: provider string → async callable(payload: dict) -> None
_handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}

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
                        try:
                            await _dispatch(provider, payload)
                            await inbox_repo.mark_processed(conn, inbox_id)
                            log.info(
                                "inbox_processed",
                                inbox_id=inbox_id,
                                provider=provider,
                            )
                            processed_count += 1
                        except Exception as exc:
                            error_str = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                            log.error(
                                "inbox_dispatch_failed",
                                inbox_id=inbox_id,
                                provider=provider,
                                attempts=attempts + 1,
                                error=str(exc),
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
