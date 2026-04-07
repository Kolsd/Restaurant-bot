"""
Lazy async Redis client for Mesio.

Behaviour
---------
- Built from the ``REDIS_URL`` environment variable.
- If ``REDIS_URL`` is unset the client is never created and every call to
  ``get_redis()`` returns ``None`` immediately.
- If a connection attempt fails, a 30-second circuit-breaker prevents hammering
  the server: the next attempt is made no sooner than 30 s after the last
  failure.
- ``close_redis()`` must be called during app shutdown (see ``app/main.py``).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import redis.asyncio as aioredis

from app.services.logging import get_logger

log = get_logger(__name__)

_REDIS_URL: str | None = os.getenv("REDIS_URL")
_CIRCUIT_BREAKER_TTL = 30  # seconds between reconnect attempts after failure

_client: aioredis.Redis | None = None
_last_failure: float = 0.0
_warned_no_url: bool = False


async def get_redis() -> aioredis.Redis | None:
    """Return a connected Redis client, or None if unavailable."""
    global _client, _last_failure, _warned_no_url

    if not _REDIS_URL:
        if not _warned_no_url:
            log.warning("redis_unavailable", reason="REDIS_URL not set — running without Redis")
            _warned_no_url = True
        return None

    if _client is not None:
        return _client

    # Circuit breaker: wait 30 s after a failure before retrying
    if _last_failure and (time.monotonic() - _last_failure) < _CIRCUIT_BREAKER_TTL:
        return None

    try:
        client = aioredis.from_url(
            _REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        # Verify connection
        await client.ping()
        _client = client
        _last_failure = 0.0
        log.info("redis_connected", url=_REDIS_URL.split("@")[-1])  # hide credentials
        return _client
    except Exception as exc:
        _last_failure = time.monotonic()
        log.warning("redis_connection_failed", error=str(exc),
                    retry_in_seconds=_CIRCUIT_BREAKER_TTL)
        return None


async def close_redis() -> None:
    """Close the Redis connection. Call during app shutdown."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
            log.info("redis_closed")
        except Exception:
            log.exception("redis_close_error")
        finally:
            _client = None


# ── JSON helpers (all values stored as JSON strings) ─────────────────────────

def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode(raw: str | None) -> Any:
    if raw is None:
        return None
    return json.loads(raw)
