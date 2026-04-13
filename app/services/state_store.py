"""
High-level state store for Mesio agent state.

Redis is the primary backend. When Redis is unavailable (REDIS_URL unset or
connection failure), an in-process dict is used as a fallback per worker. This
preserves liveness under Redis downtime at the cost of losing the multi-worker
guarantee (two workers may see inconsistent state). A rate-limited warning is
emitted at most once per 60 seconds per key family.

Key schemas
-----------
  mesio:nps:{phone}:{bot_number}           → NPS flow state dict
  mesio:nps_done:{phone}:{bot_number}      → "1" flag (12h TTL) — NPS already completed/skipped
  mesio:checkout:{phone}:{bot_number}      → checkout state machine dict
  mesio:cooldown:table:{table_id}:{bot}    → "1" (SET NX, atomic cooldown flag)
  mesio:cart_lock:{phone}:{bot_number}     → "1" (SET NX EX, distributed mutex for cart ops)

Fallback in-process dict entries are tuples of (expire_at: float, value: Any).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.services.logging import get_logger
from app.services import redis_client as _rc

log = get_logger(__name__)

# ── Fallback in-process dicts ─────────────────────────────────────────────────
# Each entry: { key: (expire_at_monotonic, value) }
_fb_nps: dict[str, tuple[float, Any]] = {}
_fb_nps_done: dict[str, float] = {}  # key → expire_at_monotonic (12h guard)
_fb_checkout: dict[str, tuple[float, Any]] = {}
_fb_cooldown: dict[str, float] = {}  # key → expire_at_monotonic
_fb_cart_locks: dict[str, asyncio.Lock] = {}  # phone:bot_number → asyncio.Lock (fallback only)

# Rate-limit fallback warnings: family → last_warned_monotonic
_fb_warn_last: dict[str, float] = {}
_FB_WARN_INTERVAL = 60.0  # seconds


def _maybe_warn(family: str) -> None:
    now = time.monotonic()
    if now - _fb_warn_last.get(family, 0.0) >= _FB_WARN_INTERVAL:
        _fb_warn_last[family] = now
        log.warning("state_store_redis_fallback",
                    family=family,
                    note="Using in-process fallback — multi-worker state consistency not guaranteed")


def _fb_get(store: dict, key: str) -> Any | None:
    entry = store.get(key)
    if entry is None:
        return None
    expire_at, value = entry
    if time.monotonic() > expire_at:
        store.pop(key, None)
        return None
    return value


def _fb_set(store: dict, key: str, value: Any, ttl_seconds: int) -> None:
    store[key] = (time.monotonic() + ttl_seconds, value)


def _fb_delete(store: dict, key: str) -> None:
    store.pop(key, None)


# ── NPS ───────────────────────────────────────────────────────────────────────

def _nps_redis_key(phone: str, bot_number: str) -> str:
    return f"mesio:nps:{phone}:{bot_number}"


async def nps_get(phone: str, bot_number: str) -> dict | None:
    key = _nps_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        raw = await r.get(key)
        return _rc.decode(raw)
    _maybe_warn("nps")
    return _fb_get(_fb_nps, key)


async def nps_set(phone: str, bot_number: str, state: dict, ttl_seconds: int = 86400) -> None:
    key = _nps_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, _rc.encode(state), ex=ttl_seconds)
        return
    _maybe_warn("nps")
    _fb_set(_fb_nps, key, state, ttl_seconds)


async def nps_delete(phone: str, bot_number: str) -> None:
    key = _nps_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.delete(key)
        return
    _maybe_warn("nps")
    _fb_delete(_fb_nps, key)


# ── NPS done flag (12h guard against re-triggering) ───────────────────────────

_NPS_DONE_TTL = 43200  # 12 hours


def _nps_done_redis_key(phone: str, bot_number: str) -> str:
    return f"mesio:nps_done:{phone}:{bot_number}"


async def nps_mark_done(phone: str, bot_number: str) -> None:
    """Mark NPS as completed/skipped for this phone+bot. Blocks re-triggering for 12h."""
    key = _nps_done_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, "1", ex=_NPS_DONE_TTL)
        return
    _maybe_warn("nps")
    now = time.monotonic()
    _fb_nps_done[key] = now + _NPS_DONE_TTL


async def nps_is_done(phone: str, bot_number: str) -> bool:
    """Returns True if NPS was already completed/skipped within the last 12h."""
    key = _nps_done_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        return await r.exists(key) == 1
    _maybe_warn("nps")
    return time.monotonic() < _fb_nps_done.get(key, 0.0)


# ── Checkout ──────────────────────────────────────────────────────────────────

def _checkout_redis_key(phone: str, bot_number: str) -> str:
    return f"mesio:checkout:{phone}:{bot_number}"


async def checkout_get(phone: str, bot_number: str) -> dict | None:
    key = _checkout_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        raw = await r.get(key)
        return _rc.decode(raw)
    _maybe_warn("checkout")
    return _fb_get(_fb_checkout, key)


async def checkout_set(phone: str, bot_number: str, state: dict, ttl_seconds: int = 1800) -> None:
    key = _checkout_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, _rc.encode(state), ex=ttl_seconds)
        return
    _maybe_warn("checkout")
    _fb_set(_fb_checkout, key, state, ttl_seconds)


async def checkout_delete(phone: str, bot_number: str) -> None:
    key = _checkout_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.delete(key)
        return
    _maybe_warn("checkout")
    _fb_delete(_fb_checkout, key)


# ── Table confirm cooldown ─────────────────────────────────────────────────────

def _cooldown_redis_key(table_id: str, bot_number: str) -> str:
    return f"mesio:cooldown:table:{table_id}:{bot_number}"


async def table_cooldown_acquire(
    table_id: str, bot_number: str, base_order_id: str = "", ttl_seconds: int = 300
) -> bool:
    """
    Acquire a cooldown lock for the given table+bot combination.

    Stores the base_order_id as the lock value so that a NEW session at the same
    table (different base_order_id) always notifies, even if the previous session's
    cooldown is still active.

    Returns True  → caller should send the WhatsApp confirmation.
    Returns False → same session, cooldown active → suppress duplicate notification.

    Redis path: GET then SET (or SET NX + compare).
    Fallback path: in-process dict with (base_order_id, expire_at) tuples.
    """
    key = _cooldown_redis_key(table_id, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        current = await r.get(key)
        if current is None:
            # No cooldown active — acquire for this session
            await r.set(key, base_order_id or "1", ex=ttl_seconds)
            return True
        stored_id = current.decode() if isinstance(current, bytes) else current
        if base_order_id and stored_id != base_order_id:
            # Different session at the same table — override cooldown and notify
            await r.set(key, base_order_id, ex=ttl_seconds)
            return True
        # Same session cooldown is active
        return False
    _maybe_warn("cooldown")
    now = time.monotonic()
    stored = _fb_cooldown.get(key)  # (base_order_id, expire_at) or float (legacy)
    if stored is None or (isinstance(stored, tuple) and now >= stored[1]):
        _fb_cooldown[key] = (base_order_id, now + ttl_seconds)
        return True
    if isinstance(stored, tuple):
        stored_id, expire_at = stored
        if base_order_id and stored_id != base_order_id:
            _fb_cooldown[key] = (base_order_id, now + ttl_seconds)
            return True
        return False  # same session, cooldown active
    # Legacy float entry
    if now >= stored:
        _fb_cooldown[key] = (base_order_id, now + ttl_seconds)
        return True
    return False


# ── Cart distributed mutex ────────────────────────────────────────────────────
# Prevents concurrent cart mutations from different workers for the same phone+bot.
# Redis path: SET key "1" NX EX ttl (atomic, multi-worker-safe).
# Fallback path: per-key asyncio.Lock (single-worker only, no cross-worker guarantee).

def _cart_lock_redis_key(phone: str, bot_number: str) -> str:
    return f"mesio:cart_lock:{phone}:{bot_number}"


async def cart_lock_acquire(phone: str, bot_number: str, ttl_seconds: int = 30) -> bool:
    """
    Acquire a distributed lock for cart operations on (phone, bot_number).

    Redis path: SET key "1" NX EX ttl — atomic, multi-worker-safe.
    Returns True if the lock was acquired, False if already held.

    Fallback (Redis unavailable): always returns True and acquires an asyncio.Lock
    instead. The asyncio.Lock is stored in _fb_cart_locks so that within a single
    worker concurrent coroutines still serialize correctly, but cross-worker
    exclusion is lost.
    """
    key = _cart_lock_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        result = await r.set(key, "1", nx=True, ex=ttl_seconds)
        return result is not None  # True → acquired, None → already held
    _maybe_warn("cart_lock")
    # Fallback: in-process asyncio.Lock
    if key not in _fb_cart_locks:
        _fb_cart_locks[key] = asyncio.Lock()
    lock = _fb_cart_locks[key]
    try:
        await asyncio.wait_for(lock.acquire(), timeout=ttl_seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def cart_lock_release(phone: str, bot_number: str) -> None:
    """
    Release a previously acquired cart lock.

    Redis path: DEL key.
    Fallback path: release the asyncio.Lock if held.
    """
    key = _cart_lock_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.delete(key)
        return
    _maybe_warn("cart_lock")
    lock = _fb_cart_locks.get(key)
    if lock is not None and lock.locked():
        lock.release()


# ── Rate limiting (sliding window) ───────────────────────────────────────────

_fb_rate_limits: dict[str, list[float]] = {}  # key → list of timestamps


async def rate_limit_check(key: str, max_requests: int, window_seconds: int) -> bool:
    """
    Check if a request should be rate-limited using a sliding window counter.

    Returns True if the request is ALLOWED, False if it should be BLOCKED.

    Redis path: Uses INCR + EXPIRE for atomic counter with TTL.
    Fallback: In-process timestamp list with expiry cleanup.
    """
    redis_key = f"mesio:ratelimit:{key}"
    r = await _rc.get_redis()
    if r is not None:
        pipe = r.pipeline()
        pipe.incr(redis_key)
        pipe.expire(redis_key, window_seconds)
        results = await pipe.execute()
        count = results[0]
        return count <= max_requests

    _maybe_warn("rate_limit")
    now = time.monotonic()
    timestamps = _fb_rate_limits.get(redis_key, [])
    # Clean expired entries
    cutoff = now - window_seconds
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    _fb_rate_limits[redis_key] = timestamps
    return len(timestamps) <= max_requests
