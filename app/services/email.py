"""app/services/email.py

Provider-agnostic transactional email sending.

Mesio is retiring the WhatsApp channel. Password resets, weekly owner
reports, and the CRM "welcome" message all need a delivery path that does
not depend on a WhatsApp Business template being approved by Meta. This
module gives the rest of the codebase a single, small interface for that:

    from app.services.email import send_email
    ok = await send_email(to=email, subject=subject, html=html, text=text)

Backend selection via the EMAIL_BACKEND env var:

    "console" (default) — logs that a send was attempted (metadata only,
                           NEVER the rendered body) and returns True. Zero
                           credentials required. This is the default
                           specifically so a missing/unset provider key can
                           never silently break a deploy: every caller still
                           gets `sent=True` and the code path is fully
                           exercised end-to-end without ever hitting a real
                           network.
    "resend"             — real HTTP delivery via https://api.resend.com
                           (uses httpx, already a project dependency — no
                           new SDK). Requires RESEND_API_KEY + EMAIL_FROM.
                           A missing key logs a WARNING and falls back to
                           the console backend rather than raising.
    "sendgrid"           — accepted as a recognized value but not wired up
                           yet (no SendGrid account exists). Falls back to
                           console with a WARNING so a typo'd/aspirational
                           env var doesn't masquerade as "delivered".

Env vars (all optional — see defaults above):
    EMAIL_BACKEND    default "console"
    RESEND_API_KEY   required only when EMAIL_BACKEND=resend
    EMAIL_FROM       default "Mesio <notificaciones@mesio.app>"

Security note — NEVER log the rendered `html`/`text` body in this module.
Callers pass password-reset codes and temporary passwords through those
fields; logging the body would defeat the masking convention used elsewhere
(see `mask_phone` / `mask_email` in app/services/logging.py) and could
defeat the Sentry `before_send` scrubber, which only scrubs by *key* name
(token/password/pin/secret/...), not by scanning free-text log bodies. Log
only the masked recipient, the subject, and body lengths.
"""
from __future__ import annotations

import os

import httpx

from app.services.logging import get_logger, mask_email

log = get_logger(__name__)

_RESEND_URL = "https://api.resend.com/emails"
_DEFAULT_FROM = "Mesio <notificaciones@mesio.app>"


async def _send_console(to: str, subject: str, html: str, text: "str | None") -> bool:
    """Local/dev backend — logs metadata only and always succeeds.

    Deliberately never logs `html`/`text` content (see module docstring).
    Logging only lengths keeps this backend useful for correctness checks
    ("did we attempt a send? to whom? what subject? roughly how big?")
    without ever becoming a secret-leak vector.
    """
    log.info(
        "email.console.send",
        to=mask_email(to),
        subject=subject,
        html_len=len(html or ""),
        text_len=len(text or ""),
    )
    return True


async def _send_resend(to: str, subject: str, html: str, text: "str | None") -> bool:
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        log.warning(
            "email.resend.missing_api_key",
            to=mask_email(to),
            subject=subject,
            hint="RESEND_API_KEY not set — falling back to console backend.",
        )
        return await _send_console(to, subject, html, text)

    from_addr = os.getenv("EMAIL_FROM", _DEFAULT_FROM)
    payload: dict = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_RESEND_URL, json=payload, headers=headers)
    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        log.warning("email.resend.network_error", to=mask_email(to), error=str(exc))
        return False
    except Exception:
        log.exception("email.resend.unexpected_error", to=mask_email(to))
        return False

    if resp.status_code in (200, 201):
        log.info("email.resend.sent", to=mask_email(to), subject=subject)
        return True

    # Resend error bodies are its own API error text (e.g. {"message": "..."})
    # — not user PII, safe to log a capped snippet for debugging.
    try:
        body_snippet = resp.text[:200]
    except Exception:
        body_snippet = None

    log.error(
        "email.resend.api_error",
        to=mask_email(to),
        status=resp.status_code,
        body_snippet=body_snippet,
    )
    return False


async def _send_sendgrid(to: str, subject: str, html: str, text: "str | None") -> bool:
    log.warning(
        "email.sendgrid.not_implemented",
        to=mask_email(to),
        hint="EMAIL_BACKEND=sendgrid selected but no SendGrid client is wired up "
             "yet (no account exists). Falling back to console. Implement "
             "_send_sendgrid or switch to EMAIL_BACKEND=resend.",
    )
    return await _send_console(to, subject, html, text)


def delivers_for_real() -> bool:
    """True only when a real provider is configured with its key. The console
    backend (and resend/sendgrid without a key, which fall back to it) only
    LOGS the message and still returns True from send_email — callers that
    must not claim "sent" (Mesio HQ alerts, the HQ password reset) check this."""
    backend = os.getenv("EMAIL_BACKEND", "console").strip().lower()
    if backend == "resend":
        return bool(os.getenv("RESEND_API_KEY", ""))
    if backend == "sendgrid":
        return bool(os.getenv("SENDGRID_API_KEY", ""))
    return False


async def send_email(to: str, subject: str, html: str, text: "str | None" = None) -> bool:
    """Send a transactional email. Never raises — returns True/False.

    Args:
        to:      recipient email address.
        subject: email subject line.
        html:    HTML body.
        text:    optional plain-text alternative (recommended — some clients
                 and spam filters penalize HTML-only email).

    Returns:
        True if the message was accepted for delivery (or logged by the
        console backend). False on a missing/invalid recipient or a
        provider/network failure. This is a best-effort side channel, not a
        transactional guarantee — callers MUST treat False as "not
        delivered" but MUST NOT let that raise or crash the caller's flow
        (password reset must still respond; the scheduler must move on to
        the next restaurant).
    """
    if not to or "@" not in to:
        log.warning("email.invalid_recipient", to=mask_email(to))
        return False

    backend = os.getenv("EMAIL_BACKEND", "console").strip().lower()

    if backend == "resend":
        return await _send_resend(to, subject, html, text)
    if backend == "sendgrid":
        return await _send_sendgrid(to, subject, html, text)
    if backend != "console":
        log.warning("email.unknown_backend", backend=backend, hint="Falling back to console.")
    return await _send_console(to, subject, html, text)
