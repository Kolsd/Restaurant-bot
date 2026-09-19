"""
app/services/realtime.py
=========================
Real-time SSE hub (Mesio) — invalidation events, not data.

Events carry only ids and a topic; clients react by re-calling the loaders
they already have (existing authorized endpoints). No personal data (names,
phones, amounts) travels through this layer — see docs/claude/architecture.md
"Real-time SSE + Redis pub/sub" and the shared sse_spec.md contract.

Two building blocks:
  - publish(org_id, topic, ...)   — fire-and-forget, never raises.
  - subscribe(org_id)             — async context manager yielding an
                                     asyncio.Queue fed with that org's events.

Transport:
  - With REDIS_URL configured: publish() does `PUBLISH mesio:rt:{org_id}
    <json>`; ONE shared `PSUBSCRIBE mesio:rt:*` connection per worker fans
    out to every local subscriber's queue (started lazily on the first
    subscriber, stopped when none remain). This is what makes cross-worker
    delivery work (4 uvicorn workers, CLAUDE.md "4 workers" rule).
  - Without Redis (local dev / tests, REDIS_URL unset or circuit-broken):
    publish() delivers directly to this worker's in-process subscribers.
    Never both paths for the same event.

Overflow: each subscriber queue has maxsize=100. If it fills (a slow /
disconnected client), the queue is drained and replaced with a single
{"topic": "resync"} marker — the client is expected to re-fetch its state
from the normal REST endpoints on resync, exactly like a fresh reconnect.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from app.services import redis_client
from app.services.logging import get_logger

log = get_logger(__name__)

# ── Topic allowlists (contract: docs/claude / sse_spec.md) ───────────────────

TOPICS = frozenset({
    "table_order.created", "table_order.updated",
    "order.created", "order.updated",
    "waiter_alert.created", "waiter_alert.updated",
    "check.updated",
    "nps.updated",
    # Delivery/pickup web wave chunk 6 (docs/claude/delivery-web.md, "Customer
    # status page") — a DEDICATED topic for the diner-facing /pedido/{code}
    # page, published ALONGSIDE (never instead of) "order.updated" on every
    # status transition. Kept separate from "order.updated" on purpose: that
    # topic is staff-only (the unfiltered /api/staff/stream — see
    # app/routes/auth_routes.py::staff_stream) and carries no ownership
    # scoping, whereas this one is DINER_ALLOWLIST-gated to the connecting
    # session's own orders (see app/routes/diner.py::_make_diner_filter).
    "delivery_order.updated",
})

# Topics a diner's own /api/diner/stream connection may ever see.
#   - table_order.*/check.updated/nps.updated: gated by an EXACT, non-None
#     table_id match against the diner session's own table (dine-in only —
#     see app/routes/diner.py::_make_diner_filter, which also fixes the bug
#     where a delivery/pickup session's table_id=None used to match ANY
#     event published without a table_id).
#   - delivery_order.updated: gated by entity_id membership in the set of
#     order ids the connecting session's own token actually owns (see
#     delivery_repo.db_get_order_ids_for_phone) — never by table_id, since a
#     delivery/pickup diner_sessions row has no table at all.
DINER_ALLOWLIST = frozenset({
    "table_order.created", "table_order.updated", "check.updated", "nps.updated",
    "delivery_order.updated",
})

_QUEUE_MAXSIZE = 100
_CHANNEL_PREFIX = "mesio:rt:"
_CHANNEL_PATTERN = "mesio:rt:*"
HEARTBEAT_SECONDS = 15.0

# org_id -> set of subscriber queues (per-worker, in-process fan-out target
# for BOTH the no-Redis path and the Redis-listener path).
_subscribers: dict[int, set["asyncio.Queue[dict]"]] = {}
_state_lock = asyncio.Lock()

_redis_listener_task: Optional[asyncio.Task] = None


def _resync_event(org_id: int) -> dict:
    return {
        "topic": "resync",
        "org_id": org_id,
        "location_id": None,
        "table_id": None,
        "entity_id": None,
        "ts": time.time(),
    }


def _push(queue: "asyncio.Queue[dict]", event: dict) -> None:
    """Best-effort enqueue. On overflow, drop everything queued and replace
    it with a single resync marker so the client knows to re-sync instead of
    silently missing updates."""
    try:
        queue.put_nowait(event)
        return
    except asyncio.QueueFull:
        pass

    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(_resync_event(event.get("org_id")))


# ── Publish ───────────────────────────────────────────────────────────────

async def publish(
    org_id: int,
    topic: str,
    *,
    location_id: Optional[int] = None,
    table_id: Optional[str] = None,
    entity_id: Optional[str] = None,
) -> None:
    """Fan out an invalidation event for *org_id*. Never raises into the caller.

    Call this AFTER the write that produced the event has committed (outside
    the transaction block) — publishing before commit could notify a client
    to re-fetch data that isn't visible yet.
    """
    if topic not in TOPICS:
        log.warning("realtime.publish.invalid_topic", topic=topic, org_id=org_id)
        return
    if not isinstance(org_id, int) or isinstance(org_id, bool) or org_id <= 0:
        log.warning("realtime.publish.invalid_org_id", org_id=org_id, topic=topic)
        return

    event = {
        "topic": topic,
        "org_id": org_id,
        "location_id": location_id,
        "table_id": table_id,
        "entity_id": entity_id,
        "ts": time.time(),
    }

    try:
        redis = await redis_client.get_redis()
    except Exception:
        log.warning("realtime.publish.redis_lookup_failed", exc_info=True)
        redis = None

    try:
        if redis is not None:
            await redis.publish(f"{_CHANNEL_PREFIX}{org_id}", json.dumps(event, ensure_ascii=False))
        else:
            await _deliver_in_process(org_id, event)
    except Exception:
        log.warning("realtime.publish.failed", topic=topic, org_id=org_id, exc_info=True)


async def publish_delivery_status(org_id: int, location_id: Optional[int], order_id: str) -> None:
    """Fan out a delivery/pickup order's status change to BOTH audiences at
    once (docs/claude/delivery-web.md, "Customer status page" — "publish it
    from EVERY status transition"):
      - "order.updated" — the existing staff-only topic (unfiltered
        /api/staff/stream) that the cashier's Domicilios queue and the
        kitchen KDS already listen on (app/routes/staff_delivery.py,
        app/repositories/orders_repo.py use this same topic for the
        `orders` table).
      - "delivery_order.updated" — the new diner-facing topic for the
        customer's own /pedido/{code} status page.
    Single call site for every transition (cashier accept/reject/assign/
    en-route/delivered AND the customer's own cancel) so the two publishes
    can never drift apart. Never raises — publish() itself never does.
    """
    await publish(org_id, "order.updated", location_id=location_id, entity_id=order_id)
    await publish(org_id, "delivery_order.updated", location_id=location_id, entity_id=order_id)


async def _deliver_in_process(org_id: int, event: dict) -> None:
    async with _state_lock:
        queues = list(_subscribers.get(org_id, ()))
    for q in queues:
        _push(q, event)


# ── Subscribe ─────────────────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def subscribe(org_id: int) -> AsyncIterator["asyncio.Queue[dict]"]:
    """Yield an asyncio.Queue fed with *org_id*'s events for the lifetime of
    the `async with` block. Starts the shared Redis listener lazily on the
    first subscriber (any org, this worker) and stops it once the last one
    leaves."""
    queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    async with _state_lock:
        _subscribers.setdefault(org_id, set()).add(queue)
        await _ensure_redis_listener_locked()
    try:
        yield queue
    finally:
        async with _state_lock:
            subs = _subscribers.get(org_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    del _subscribers[org_id]
            await _maybe_stop_redis_listener_locked()


def subscriber_count(org_id: Optional[int] = None) -> int:
    """Introspection helper for tests: total subscribers, or for one org."""
    if org_id is not None:
        return len(_subscribers.get(org_id, ()))
    return sum(len(v) for v in _subscribers.values())


# ── Shared Redis PSUBSCRIBE connection (per worker) ──────────────────────────

async def _ensure_redis_listener_locked() -> None:
    """Must be called with _state_lock held."""
    global _redis_listener_task
    if _redis_listener_task is not None and not _redis_listener_task.done():
        return
    redis = await redis_client.get_redis()
    if redis is None:
        return  # no Redis configured / circuit-broken — in-process delivery only
    _redis_listener_task = asyncio.create_task(_redis_listen_loop(), name="mesio-realtime-listener")


async def _maybe_stop_redis_listener_locked() -> None:
    """Must be called with _state_lock held."""
    global _redis_listener_task
    if _subscribers:
        return
    task = _redis_listener_task
    _redis_listener_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _redis_listen_loop() -> None:
    redis = await redis_client.get_redis()
    if redis is None:
        return
    pubsub = redis.pubsub()
    try:
        await pubsub.psubscribe(_CHANNEL_PATTERN)
        async for message in pubsub.listen():
            if message is None or message.get("type") != "pmessage":
                continue
            try:
                event = json.loads(message["data"])
            except (ValueError, TypeError):
                log.warning("realtime.listener.bad_payload")
                continue
            org_id = event.get("org_id")
            if org_id is None:
                continue
            try:
                org_id = int(org_id)
            except (TypeError, ValueError):
                continue
            async with _state_lock:
                queues = list(_subscribers.get(org_id, ()))
            for q in queues:
                _push(q, event)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("realtime.listener.crashed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.punsubscribe(_CHANNEL_PATTERN)
        with contextlib.suppress(Exception):
            await pubsub.aclose()


async def shutdown() -> None:
    """Stop the Redis listener task, if running. Call on app shutdown."""
    global _redis_listener_task
    task = _redis_listener_task
    _redis_listener_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


# ── SSE wire format ───────────────────────────────────────────────────────

def _format_sse(event_name: str, data: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_stream(
    is_disconnected: Callable[[], Awaitable[bool]],
    org_id: int,
    *,
    topic_filter: Optional[Callable[[dict], bool]] = None,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """Core SSE generator, decoupled from FastAPI so it can be driven directly
    in tests with asyncio.wait_for timeouts instead of an endless HTTP call.

    - First frame: `event: ready`.
    - Then either a filtered event frame or a `: ping\\n\\n` heartbeat comment
      every *heartbeat_seconds*.
    - `resync` events always pass *topic_filter* (they carry no data to
      filter on and every client needs to know to re-sync).
    - Stops as soon as `is_disconnected()` returns True.
    """
    async with subscribe(org_id) as queue:
        yield "event: ready\ndata: {}\n\n"
        last_beat = time.monotonic()
        while True:
            if await is_disconnected():
                return
            remaining = heartbeat_seconds - (time.monotonic() - last_beat)
            if remaining <= 0:
                yield ": ping\n\n"
                last_beat = time.monotonic()
                continue
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue

            topic = event.get("topic", "message")
            if topic_filter is not None and topic != "resync" and not topic_filter(event):
                continue
            yield _format_sse(topic, event)
