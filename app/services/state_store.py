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
import copy
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
_fb_nps_transition_locks: dict[str, asyncio.Lock] = {}  # nps_lock key → asyncio.Lock (fallback only)
_fb_nps_transition_owner: dict[str, str] = {}  # nps_lock key → owner token (fallback only)

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


def _fb_enforce_size_cap(store: dict, family: str = "unknown") -> None:
    if len(store) <= _FB_MAX_SIZE:
        return
    _fb_purge_expired(store)
    if len(store) > _FB_MAX_SIZE:
        _maybe_warn(f"{family}_cap")
        log.warning("state_store.fallback_capacity_exceeded",
                    family=family, current_size=len(store), max_size=_FB_MAX_SIZE)
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


def _fb_set(store: dict, key: str, value: Any, ttl_seconds: int, family: str = "unknown") -> None:
    _fb_enforce_size_cap(store, family)
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
    value = _fb_get(_fb_nps, key)
    # deepcopy so callers cannot mutate shared fallback state across workers
    return copy.deepcopy(value) if isinstance(value, dict) else value


async def nps_set(phone: str, bot_number: str, state: dict, ttl_seconds: int = 86400) -> None:
    key = _nps_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, _rc.encode(state), ex=ttl_seconds)
        return
    _maybe_warn("nps")
    _fb_set(_fb_nps, key, state, ttl_seconds, family="nps")


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
            _maybe_warn("nps_done_cap")
            log.warning("state_store.fallback_capacity_exceeded",
                        family="nps_done", current_size=len(_fb_nps_done), max_size=_FB_MAX_SIZE)
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
    value = _fb_get(_fb_checkout, key)
    # deepcopy so callers cannot mutate shared fallback state across workers
    return copy.deepcopy(value) if isinstance(value, dict) else value


async def checkout_set(phone: str, bot_number: str, state: dict, ttl_seconds: int = 1800) -> None:
    key = _checkout_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, _rc.encode(state), ex=ttl_seconds)
        return
    _maybe_warn("checkout")
    _fb_set(_fb_checkout, key, state, ttl_seconds, family="checkout")


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
            _maybe_warn("cooldown_cap")
            log.warning("state_store.fallback_capacity_exceeded",
                        family="cooldown", current_size=len(_fb_cooldown), max_size=_FB_MAX_SIZE)
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


# ── NPS transition distributed lock ──────────────────────────────────────────


async def nps_transition_lock_acquire(phone: str, bot_number: str, ttl_seconds: int = 10) -> str | None:
    """Acquire atomic lock for NPS state transition. Returns token or None.

    Redis path: SET NX — only one worker acquires; others get None.
    Fallback path: asyncio.Lock per key (same as cart_lock_acquire) — prevents
    double-fire within a single worker. Returns None on timeout (5s cap).
    """
    key = f"mesio:nps_lock:{phone}:{bot_number}"
    token = str(uuid.uuid4())
    r = await _rc.get_redis()
    if r is not None:
        try:
            result = await r.set(key, token, nx=True, ex=ttl_seconds)
            return token if result is not None else None
        except Exception:
            _maybe_warn("nps_lock")
            # Redis error mid-flight — fall through to in-process lock so the
            # caller still gets a serialised path within this worker.
    # Fallback: in-process asyncio.Lock (single-worker safe only)
    _maybe_warn("nps_lock")
    if key not in _fb_nps_transition_locks:
        _fb_nps_transition_locks[key] = asyncio.Lock()
    lock = _fb_nps_transition_locks[key]
    try:
        await asyncio.wait_for(lock.acquire(), timeout=5.0)
        _fb_nps_transition_owner[key] = token
        return token
    except (asyncio.TimeoutError, Exception):
        return None


async def nps_transition_lock_release(phone: str, bot_number: str, token: str) -> bool:
    """Release a previously acquired NPS transition lock. Ownership-safe via Lua.

    Fallback path mirrors cart_lock_release: verifies token before releasing.
    """
    key = f"mesio:nps_lock:{phone}:{bot_number}"
    r = await _rc.get_redis()
    if r is not None:
        try:
            # Lua script: only DEL if our token matches — prevents releasing another worker's lock
            script = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
            result = await r.eval(script, 1, key, token)
            return int(result) == 1
        except Exception:
            _maybe_warn("nps_lock_release")
            return False
    # Fallback: release asyncio.Lock only if this token is the current owner
    _maybe_warn("nps_lock")
    if _fb_nps_transition_owner.get(key) != token:
        log.warning("nps_lock.release_ownership_mismatch_fallback", key=key)
        return False
    _fb_nps_transition_owner.pop(key, None)
    lock = _fb_nps_transition_locks.get(key)
    if lock is not None and lock.locked():
        lock.release()
    # Clean up the lock object if no longer held
    lock = _fb_nps_transition_locks.get(key)
    if lock is not None and not lock.locked():
        _fb_nps_transition_locks.pop(key, None)
    return True


# ── Rate limiting (fixed window) ─────────────────────────────────────────────

_fb_rate_limits: dict[str, list[float]] = {}  # key → list of timestamps


async def rate_limit_check(key: str, max_requests: int, window_seconds: int) -> bool:
    """
    Fixed window rate limiter in Redis (INCR+EXPIRE). In-process fallback uses sliding window.

    Returns True if the request is ALLOWED, False if it should be BLOCKED.

    Redis path: Uses INCR + EXPIRE for atomic counter with TTL (fixed window).
    Fallback: In-process timestamp list with expiry cleanup (sliding window).
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

_SCHEDULER_LEADER_KEY = "mesio:scheduler_leader"

# Lua: compare-and-PEXPIRE (renew TTL only if caller owns the lock).
# Returns 1 if renewed, 0 if mismatch (no longer leader).
_LUA_RENEW = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "  return redis.call('PEXPIRE', KEYS[1], ARGV[2]) "
    "else "
    "  return 0 "
    "end"
)


async def scheduler_leader_acquire(ttl_seconds: int = 90) -> str | None:
    """
    Try to acquire the scheduler leader lock.

    Only one worker should run the scheduler tick. Uses Redis SET NX with a TTL
    slightly longer than the scheduler tick interval (60s tick → 90s TTL).

    Returns the owner token (str) if this worker is the leader, None otherwise.
    Callers that only check truthiness (``if not token:``) keep working unchanged.
    Falls back to a constant sentinel token "fallback" when Redis is unavailable
    (single-process mode — no race condition in that case).
    """
    key = _SCHEDULER_LEADER_KEY
    token = str(uuid.uuid4())
    r = await _rc.get_redis()
    if r is not None:
        result = await r.set(key, token, nx=True, ex=ttl_seconds)
        if result is not None:
            log.info("scheduler.leader_acquired", token=token[:8])
            return token
        return None  # lock already held by another worker
    # No Redis → fall back to running on all workers (degraded but functional)
    _maybe_warn("scheduler_leader")
    return "fallback"


async def scheduler_leader_renew(token: str, ttl_seconds: int = 90) -> bool:
    """
    Extend the scheduler leader lease if this worker still owns it.

    Uses an atomic Lua script: GET key → compare → PEXPIRE.
    Returns True  → lease renewed, this worker is still leader.
    Returns False → lease was lost (another worker took over); caller should abort the tick.

    Fallback (no Redis / "fallback" sentinel): always returns True.
    Only the single-process case reaches here with token=="fallback"; no race possible.
    """
    if token == "fallback":
        # No Redis in use — single process, can't lose the lease.
        return True

    key = _SCHEDULER_LEADER_KEY
    r = await _rc.get_redis()
    if r is not None:
        try:
            ttl_ms = ttl_seconds * 1000
            result = await r.eval(_LUA_RENEW, 1, key, token, str(ttl_ms))
            renewed = int(result) == 1
            if not renewed:
                log.warning("scheduler.lease_lost_mid_tick", token=token[:8])
            else:
                log.debug("scheduler.lease_renewed", token=token[:8], ttl_seconds=ttl_seconds)
            return renewed
        except Exception as exc:
            log.error("scheduler.leader_renew_failed", error=str(exc))
            # On Redis error mid-tick, assume we're still leader to avoid
            # silently abandoning a partially complete tick.  The lease will
            # expire normally if Redis stays down.
            return True
    # No Redis reachable — same as fallback token, no race.
    return True


# ── Join-code pending (Capa 2 — multi-participant table join) ────────────────
# State shape: {"table_id": "...", "table_name": "...", "attempts": 0,
#               "org_id": int, "location_id": int | None}
# Key: mesio:join_code_pending:{phone}:{bot_number}
# TTL: 600 seconds (10 min — long enough for the group to share the code)

_fb_join_code_pending: dict[str, tuple[float, Any]] = {}
_JOIN_CODE_PENDING_TTL = 600  # 10 minutes


def _join_code_pending_redis_key(phone: str, bot_number: str) -> str:
    return f"mesio:join_code_pending:{phone}:{bot_number}"


async def join_code_pending_get(phone: str, bot_number: str) -> dict | None:
    """Return the pending join-code state for this phone+bot, or None."""
    key = _join_code_pending_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        raw = await r.get(key)
        return _rc.decode(raw)
    _maybe_warn("join_code_pending")
    value = _fb_get(_fb_join_code_pending, key)
    return copy.deepcopy(value) if isinstance(value, dict) else value


async def join_code_pending_set(
    phone: str, bot_number: str, state: dict, ttl_seconds: int = _JOIN_CODE_PENDING_TTL
) -> None:
    """Persist the pending join-code state (overwrites existing). state MUST NOT
    contain Decimal values — use plain int/str/None (Rule #1)."""
    key = _join_code_pending_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.set(key, _rc.encode(state), ex=ttl_seconds)
        return
    _maybe_warn("join_code_pending")
    _fb_set(_fb_join_code_pending, key, state, ttl_seconds, family="join_code_pending")


async def join_code_pending_delete(phone: str, bot_number: str) -> None:
    """Clear the pending join-code state for this phone+bot."""
    key = _join_code_pending_redis_key(phone, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        await r.delete(key)
        return
    _maybe_warn("join_code_pending")
    _fb_delete(_fb_join_code_pending, key)


# ── Scheduler heartbeat ───────────────────────────────────────────────────────

_SCHEDULER_HEARTBEAT_KEY = "mesio:scheduler:heartbeat"
_SCHEDULER_HEARTBEAT_TTL = 300  # 5 minutes — stale if no tick in >5 min


async def set_scheduler_heartbeat() -> None:
    """Write current UTC timestamp to scheduler heartbeat key. TTL 5 min. Best-effort."""
    r = await _rc.get_redis()
    if r is None:
        return
    try:
        await r.set(_SCHEDULER_HEARTBEAT_KEY, str(int(time.time())), ex=_SCHEDULER_HEARTBEAT_TTL)
    except Exception:
        pass  # best-effort — do not crash the scheduler tick


async def get_scheduler_heartbeat() -> int | None:
    """Return last tick UNIX timestamp, or None if Redis unavailable or key expired."""
    r = await _rc.get_redis()
    if r is None:
        return None
    try:
        val = await r.get(_SCHEDULER_HEARTBEAT_KEY)
        return int(val) if val else None
    except Exception:
        return None
