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

Fallback in-process dict entries are tuples of (expire_at: float, value: Any).
"""

from __future__ import annotations

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


async def table_cooldown_acquire(table_id: str, bot_number: str, ttl_seconds: int = 300) -> bool:
    """
    Atomically acquire a cooldown lock for the given table+bot combination.

    Returns True if the lock was acquired (no active cooldown → proceed with
    confirmation). Returns False if a cooldown is already active (suppress the
    WhatsApp message).

    Redis path: SET key "1" NX EX ttl — atomic, works across workers.
    Fallback path: checks/updates the in-process _fb_cooldown dict.
    """
    key = _cooldown_redis_key(table_id, bot_number)
    r = await _rc.get_redis()
    if r is not None:
        result = await r.set(key, "1", ex=ttl_seconds, nx=True)
        # redis-py returns True when SET NX succeeded (key was new), None otherwise
        return result is True
    _maybe_warn("cooldown")
    now = time.monotonic()
    expire_at = _fb_cooldown.get(key, 0.0)
    if now < expire_at:
        return False  # cooldown still active
    _fb_cooldown[key] = now + ttl_seconds
    return True
