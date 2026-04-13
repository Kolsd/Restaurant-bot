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
import uuid
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
_fb_cart_lock_tokens: dict[str, str] = {}  # key → owner token (fallback only)

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


_FB_MAX_SIZE = 10_000


def _fb_purge_expired(store: dict) -> None:
    now = time.monotonic()
    expired = [k for k, v in store.items() if (v[0] if isinstance(v, tuple) else v) <= now]
    for k in expired:
        store.pop(k, None)


def _fb_enforce_size_cap(store: dict) -> None:
    if len(store) <= _FB_MAX_SIZE:
        return
    _fb_purge_expired(store)
    if len(store) > _FB_MAX_SIZE:
        drop = sorted(store.items(), key=lambda x: x[1][0] if isinstance(x[1], tuple) else x[1])
        for k, _ in drop[: len(store) // 2]:
            store.pop(k, None)


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
    _fb_enforce_size_cap(store)
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
    if len(_fb_nps_done) >= _FB_MAX_SIZE:
        expired = [k for k, exp in _fb_nps_done.items() if now >= exp]
        for k in expired:
            _fb_nps_done.pop(k, None)
        if len(_fb_nps_done) >= _FB_MAX_SIZE:
            drop = sorted(_fb_nps_done.items(), key=lambda x: x[1])
            for k, _ in drop[: len(_fb_nps_done) // 2]:
                _fb_nps_done.pop(k, None)
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
        stored_id = current
        if base_order_id and stored_id != base_order_id:
            # Different session at the same table — override cooldown and notify
            await r.set(key, base_order_id, ex=ttl_seconds)
            return True
        # Same session cooldown is active
        return False
    _maybe_warn("cooldown")
    now = time.monotonic()
    if len(_fb_cooldown) >= _FB_MAX_SIZE:
        expired = [
            k for k, v in _fb_cooldown.items()
            if now >= (v[1] if isinstance(v, tuple) else v)
        ]
        for k in expired:
            _fb_cooldown.pop(k, None)
        if len(_fb_cooldown) >= _FB_MAX_SIZE:
            drop = sorted(_fb_cooldown.items(), key=lambda x: x[1][1] if isinstance(x[1], tuple) else x[1])
            for k, _ in drop[: len(_fb_cooldown) // 2]:
                _fb_cooldown.pop(k, None)
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


async def cart_lock_acquire(phone: str, bot_number: str, ttl_seconds: int = 30) -> str | None:
    """
    Acquire a distributed lock for cart operations on (phone, bot_number).

    Redis path: SET key <token> NX EX ttl — atomic, multi-worker-safe.
    Returns the lock token (str) if acquired, None if already held.
    The caller must pass the returned token to cart_lock_release.

    Fallback (Redis unavailable): acquires an asyncio.Lock instead and returns
    a token for ownership tracking within a single worker.
    """
    key = _cart_lock_redis_key(phone, bot_number)
    token = str(uuid.uuid4())
    r = await _rc.get_redis()
    if r is not None:
        result = await r.set(key, token, nx=True, ex=ttl_seconds)
        return token if result is not None else None
    _maybe_warn("cart_lock")
    # Fallback: in-process asyncio.Lock
    if key not in _fb_cart_locks:
        _fb_cart_locks[key] = asyncio.Lock()
    lock = _fb_cart_locks[key]
    try:
        await asyncio.wait_for(lock.acquire(), timeout=5.0)
        _fb_cart_lock_tokens[key] = token
        return token
    except asyncio.TimeoutError:
        return None


async def cart_lock_release(phone: str, bot_number: str, token: str | None = None) -> None:
    """
    Release a previously acquired cart lock.

    Redis path: only DEL if the stored token matches (ownership check).
    Fallback path: release the asyncio.Lock only if this token is the holder.
    """
    key = _cart_lock_redis_key(phone, bot_number)
    if token is None:
        log.error("cart_lock.release_without_token", key=key)
        return
    r = await _rc.get_redis()
    if r is not None:
        stored = await r.get(key)
        stored_str = stored
        if stored_str != token:
            log.warning("cart_lock.release_ownership_mismatch", key=key)
            return
        await r.delete(key)
        return
    _maybe_warn("cart_lock")
    if _fb_cart_lock_tokens.get(key) != token:
        log.warning("cart_lock.release_ownership_mismatch_fallback", key=key)
        return
    _fb_cart_lock_tokens.pop(key, None)
    lock = _fb_cart_locks.get(key)
    if lock is not None and lock.locked():
        lock.release()
    lock = _fb_cart_locks.get(key)
    if lock is not None and not lock.locked():
        _fb_cart_locks.pop(key, None)
        _fb_cart_lock_tokens.pop(key, None)


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
        count = await r.incr(redis_key)
        if count == 1:
            await r.expire(redis_key, window_seconds)
        return count <= max_requests

    _maybe_warn("rate_limit")
    now = time.monotonic()
    if len(_fb_rate_limits) >= _FB_MAX_SIZE:
        now = time.monotonic()
        expired = [k for k, v in _fb_rate_limits.items() if not v or max(v) < now - 60]
        for k in expired:
            _fb_rate_limits.pop(k, None)
        if len(_fb_rate_limits) >= _FB_MAX_SIZE:
            by_oldest = sorted(_fb_rate_limits.items(), key=lambda x: min(x[1]) if x[1] else 0)
            for k, _ in by_oldest[: len(_fb_rate_limits) // 2]:
                _fb_rate_limits.pop(k, None)
    timestamps = _fb_rate_limits.get(redis_key, [])
    cutoff = now - window_seconds
    timestamps = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    _fb_rate_limits[redis_key] = timestamps
    return len(timestamps) <= max_requests


# ── Scheduler leader election ────────────────────────────────────────────────

async def scheduler_leader_acquire(ttl_seconds: int = 90) -> bool:
    """
    Try to acquire the scheduler leader lock.

    Only one worker should run the scheduler tick. Uses Redis SET NX with a TTL
    slightly longer than the scheduler tick interval (60s tick → 90s TTL).

    Returns True if this worker is the leader, False otherwise.
    Falls back to True (run everywhere) if Redis is unavailable.
    """
    key = "mesio:scheduler_leader"
    r = await _rc.get_redis()
    if r is not None:
        result = await r.set(key, "1", nx=True, ex=ttl_seconds)
        if result is not None:
            log.info("scheduler.leader_acquired")
        return result is not None  # True → acquired, None → already held
    # No Redis → fall back to running on all workers (degraded but functional)
    _maybe_warn("scheduler_leader")
    return True
