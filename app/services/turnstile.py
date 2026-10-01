"""
app/services/turnstile.py
==========================
Cloudflare Turnstile verification for the delivery/pickup diner entry flow
(docs/claude/delivery-web.md — "Cloudflare Turnstile"). Wired into
POST /api/diner/session for order_mode delivery/pickup ONLY — the dine-in QR
path never gets a captcha (scanning a physical table QR is already a strong
signal the dine-in flow doesn't need to add friction to).

TURNSTILE_SECRET unset -> verify() is a NO-OP that returns success. This is
intentional for local dev and the test suite (docs/claude/env.md) — the rate
limits in app/routes/diner.py / diner_delivery.py still apply regardless.
The no-op state is logged ONCE at process startup (see log_startup_state(),
called from app/main.py's lifespan), never per request — a per-request log
line would spam every single delivery/pickup session open in any environment
that hasn't configured Turnstile yet.
"""
from __future__ import annotations

import os

import httpx

from app.services.logging import get_logger

log = get_logger(__name__)

_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 8


def is_configured() -> bool:
    return bool(os.getenv("TURNSTILE_SECRET", "").strip())


def get_site_key() -> str:
    """The Turnstile SITE key — safe to hand to the browser (unlike
    TURNSTILE_SECRET, which never leaves this module). Empty string when
    unset, e.g. local/test — the ordering page then skips rendering the
    widget entirely and this module's no-op verify() path applies."""
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


async def verify(token: str, remote_ip: str | None = None) -> bool:
    """Verify a Turnstile response token against Cloudflare's siteverify API.

    Returns True (no-op success) when TURNSTILE_SECRET is unset — see module
    docstring. Otherwise fails CLOSED: a missing token, a non-200 response,
    a transport error, or an unparseable body all return False. Never raises
    — callers get a plain bool to turn into a 422, matching the rest of this
    codebase's "never leave the customer with a 500 for an external service
    hiccup" convention (see e.g. app/services/meta_api.py).
    """
    secret = os.getenv("TURNSTILE_SECRET", "").strip()
    if not secret:
        return True

    if not token:
        return False

    payload = {"secret": secret, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(_SITEVERIFY_URL, data=payload)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        log.warning("turnstile.verify_transport_error", error=str(exc))
        return False

    if resp.status_code != 200:
        log.warning("turnstile.verify_http_error", status=resp.status_code)
        return False

    try:
        body = resp.json()
    except ValueError:
        log.warning("turnstile.verify_bad_json")
        return False

    return bool(body.get("success"))


def log_startup_state() -> None:
    """Log the Turnstile configuration state ONCE at process startup
    (called from app/main.py's lifespan) — never per request."""
    if is_configured():
        log.info("startup.turnstile_configured")
    else:
        log.warning(
            "startup.turnstile_unset",
            message=(
                "TURNSTILE_SECRET not set — delivery/pickup session "
                "verification is a no-op (expected in local/test only)."
            ),
        )
