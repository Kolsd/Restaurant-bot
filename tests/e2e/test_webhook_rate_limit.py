"""
tests/e2e/test_webhook_rate_limit.py — E2E test: global webhook rate limiter.

The global rate limiter at POST /webhook/meta:
  rate_limit_check("webhook_global", max_requests=200, window_seconds=1)

When triggered:
  - Returns HTTP 200 with {"status": "ok"} (NOT 429 — avoids Meta retry storms)
  - Does NOT enqueue into webhook_inbox (the request is dropped early)
  - Logs webhook.global_rate_limit

This test fires 250 concurrent webhooks in a burst (same 1-second window) and
asserts that fewer than 250 rows appear in webhook_inbox — proving the limiter
dropped some requests.

IMPORTANT: All responses are HTTP 200. The rate limiter intentionally returns
200 to prevent Meta from retrying. The distinction between "allowed" and
"limited" is the absence of a webhook_inbox row, not the HTTP status code.

Strategy:
  - Fire 250 POST /webhook/meta with UNIQUE wam_ids (avoids dedup collisions)
  - Each payload has a unique phone + wam_id so no dedup or rate limiting
    by phone obscures the test
  - After all 250 POSTs, COUNT webhook_inbox rows
  - Assert COUNT < 250 (some were rate-limited and not enqueued)
  - Assert all 250 returned HTTP 200 (the limiter always returns 200)
  - Assert the 250 POSTs completed in < 5 seconds

NOTE: Without Redis, the in-process fallback is a sliding window. With 250
concurrent asyncio coroutines, the window is tight but realistic. The in-process
fallback _is_ async-safe within a single event loop. If Redis is available, the
INCR counter provides stricter guarantees.

If all 250 return 200 AND 250 rows appear in webhook_inbox (no rate limiting),
that is a GAP — the rate limiter is not firing.

Run:
  pytest tests/e2e/test_webhook_rate_limit.py -v -m e2e --tb=short
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import uuid

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    _build_meta_signature,
    _normalize_phone,
)
from app.services.logging import get_logger

log = get_logger(__name__)

NUM_REQUESTS = 250
RATE_LIMIT = 200  # max_requests from chat.py
BOT_NUMBER = "570E2ERATELMT1"  # no '+' prefix — normalized already


# ── App fixture ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


def _make_burst_webhook(index: int) -> bytes:
    """
    Build a unique webhook payload for the burst test.
    Each request has a unique wam_id and sender phone to avoid dedup/per-phone
    rate limits obscuring the global rate limit result.
    """
    uid = uuid.uuid4().hex
    wam_id = f"wamid.burst_{uid}"
    sender = f"5730000{index:05d}"  # unique phone per request

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "burst_entry",
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "display_phone_number": BOT_NUMBER,
                                "phone_number_id": "burst_phone_id",
                            },
                            "messages": [
                                {
                                    "id": wam_id,
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": f"burst test {index}"},
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


async def _post_one(client: AsyncClient, index: int) -> int:
    """Fire a single webhook and return the HTTP status code."""
    body = _make_burst_webhook(index)
    app_secret = os.environ.get("META_APP_SECRET", "test_secret_e2e")
    sig = _build_meta_signature(body, app_secret)
    resp = await client.post(
        "/api/webhook/meta",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    return resp.status_code


# ── Test ───────────────────────────────────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_webhook_global_rate_limit_drops_burst(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
):
    """
    Fires 250 concurrent webhooks and asserts the rate limiter dropped some.

    The rate limit is 200 req/s. With 250 concurrent requests in the same
    event loop tick, at least 50 should be blocked.

    All responses must be HTTP 200 (rate limiter returns 200, not 429).
    webhook_inbox row count must be < 250 (blocked requests were not enqueued).

    If all 250 are enqueued (COUNT == 250), the rate limiter is not firing.
    """
    client = e2e_app
    pool = test_pool

    # Clear any lingering rate-limit state from prior tests
    try:
        import app.services.state_store as ss
        fb = getattr(ss, "_fb_rate_limits", None)
        if fb is not None:
            fb.clear()
    except Exception:
        pass

    # Also clear webhook_inbox of any prior test rows so our count is accurate
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM webhook_inbox WHERE payload IS NOT NULL")

    # ── Fire 250 concurrent webhooks ──────────────────────────────────────────
    t0 = time.monotonic()
    statuses = await asyncio.gather(
        *[_post_one(client, i) for i in range(NUM_REQUESTS)],
        return_exceptions=False,
    )
    elapsed = time.monotonic() - t0

    log.info(
        "e2e.rate_limit.burst_done",
        num_requests=NUM_REQUESTS,
        elapsed_s=round(elapsed, 2),
        status_counts={s: statuses.count(s) for s in set(statuses)},
    )

    # ── ASSERTION 1: All responses are HTTP 200 ───────────────────────────────
    # The rate limiter returns 200 (not 429) to avoid Meta retry storms.
    non_200 = [s for s in statuses if s != 200]
    assert len(non_200) == 0, (
        f"Expected all {NUM_REQUESTS} responses to be HTTP 200. "
        f"Got non-200 responses: {non_200[:10]}. "
        f"The rate limiter should always return 200, never 4xx/5xx."
    )
    log.info("e2e.rate_limit.assert_all_200_ok")

    # ── ASSERTION 2: Completed within 30 seconds ─────────────────────────────
    # 250 concurrent ASGI requests share a single DB pool (max_size=10).
    # Non-rate-limited requests still attempt DB calls (dedup check) and may
    # queue briefly. Allow 30s for all 250 to complete.
    assert elapsed < 30.0, (
        f"250 concurrent webhook POSTs took {elapsed:.1f}s — expected < 30s. "
        f"Something is hanging unexpectedly."
    )
    log.info("e2e.rate_limit.assert_timing_ok", elapsed=elapsed)

    # ── ASSERTION 3: webhook_inbox received < 250 rows ────────────────────────
    # Rate-limited requests are dropped before enqueue — they produce no inbox row.
    async with pool.acquire() as conn:
        inbox_count = await conn.fetchval(
            "SELECT COUNT(*) FROM webhook_inbox WHERE payload IS NOT NULL"
        )

    log.info(
        "e2e.rate_limit.inbox_count",
        count=inbox_count,
        rate_limit=RATE_LIMIT,
        num_requests=NUM_REQUESTS,
    )

    if inbox_count >= NUM_REQUESTS:
        # All 250 were enqueued — rate limiter did not fire.
        # This can happen if:
        # 1. The async event loop serialized requests slowly enough that they
        #    all fell into different 1-second windows (unlikely with 250 concurrent)
        # 2. The in-process fallback has a bug
        # 3. The Redis rate limiter reset between requests
        #
        # We FAIL the test to surface this as a GAP.
        pytest.fail(
            f"GAP: Rate limiter did not drop any requests. "
            f"webhook_inbox has {inbox_count} rows (expected < {NUM_REQUESTS}). "
            f"The global rate limiter at rate_limit_check('webhook_global', "
            f"max_requests=200, window_seconds=1) should have blocked "
            f"{NUM_REQUESTS - RATE_LIMIT} of the {NUM_REQUESTS} burst requests. "
            f"Check: state_store.rate_limit_check(), chat.py line ~300, "
            f"and whether REDIS_URL is configured (in-process fallback may behave "
            f"differently if requests span > 1 second in a slow event loop)."
        )

    # At least some requests were dropped
    dropped = NUM_REQUESTS - inbox_count
    assert dropped >= 1, (
        f"Expected at least 1 request to be rate-limited. "
        f"Got inbox_count={inbox_count} out of {NUM_REQUESTS} requests. "
        f"Rate limiter may not be active."
    )

    log.info(
        "e2e.rate_limit.PASS",
        total=NUM_REQUESTS,
        enqueued=inbox_count,
        dropped=dropped,
        elapsed=elapsed,
    )

    print(
        f"\n[OK] Global webhook rate limiter is active.\n"
        f"  {NUM_REQUESTS} burst requests fired in {elapsed:.2f}s\n"
        f"  Enqueued in webhook_inbox: {inbox_count}\n"
        f"  Rate-limited (dropped): {dropped}\n"
        f"  All responses: HTTP 200 (correct — limiter never returns 4xx)"
    )
