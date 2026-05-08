"""
app/services/sentry.py

Sentry error-tracking initializer for Mesio.

Usage:
    from app.services.sentry import init_sentry
    init_sentry("web")          # in app/main.py lifespan
    init_sentry("inbox_worker") # in scripts/run_inbox_worker.py

Design:
- No-op (returns False) when SENTRY_DSN is not set. Zero overhead.
- send_default_pii=False — phone numbers and WhatsApp tokens are scrubbed
  by the before_send hook before any event leaves the process.
- traces_sample_rate defaults to 0.1 (10 %) to stay within free-tier budgets.
- profiling is explicitly off (profiles_sample_rate=0.0).
- LoggingIntegration captures structlog ERROR+ as Sentry events and INFO+
  as breadcrumbs automatically (structlog routes through stdlib LoggerFactory).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.asyncpg import AsyncPGIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.services.logging import get_logger
from app.services.tenant_context import peek_tenant

_log = get_logger(__name__)

# Compiled once at module load — cheap and thread-safe.
# Matches Colombian mobile/landline numbers: optional +57 prefix, 10-13 digits.
_PHONE_RE = re.compile(r"(?:\+?57)?[\s\-]?\d{10,13}")
# Meta access tokens start with EAA followed by 20+ alphanumeric characters.
_META_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]{20,}")
# Key-name fragments that indicate the value is sensitive — redact regardless of content.
_SENSITIVE_KEY_FRAGMENTS = frozenset(
    {"phone", "email", "token", "password", "passwd", "pin", "secret", "key"}
)


def _scrub_string(value: str) -> str:
    """Replace PII patterns in a single string."""
    value = _PHONE_RE.sub("[phone-redacted]", value)
    value = _META_TOKEN_RE.sub("[meta-token-redacted]", value)
    return value


def _key_is_sensitive(key: str) -> bool:
    """Return True if the key name (case-insensitive) contains a PII/secrets fragment."""
    key_lower = key.lower()
    return any(frag in key_lower for frag in _SENSITIVE_KEY_FRAGMENTS)


def _scrub_dict_values(d: dict) -> None:
    """Mutate a dict in-place, scrubbing string values.

    Two-pass strategy:
    1. Key-name: if the key signals PII/secrets (phone, email, token, password,
       pin, secret, key), replace the value with "[redacted]" entirely.
    2. Value-regex: scrub phone numbers and Meta tokens from remaining strings.
    """
    for key in list(d):
        if isinstance(d[key], str):
            if _key_is_sensitive(key):
                d[key] = "[redacted]"
            else:
                d[key] = _scrub_string(d[key])


def _make_before_send(component: str):
    """Return a before_send hook closed over *component*."""

    def _scrub_and_tag(event: dict, hint: Any) -> dict:  # noqa: ANN001
        try:
            # ── Tag with active org_id (non-raising) ─────────────────────────
            org_id = peek_tenant()
            tags = event.setdefault("tags", {})
            if org_id is not None:
                tags["org_id"] = str(org_id)

            # ── Tag with component ────────────────────────────────────────────
            tags["component"] = component

            # ── Scrub event.message ───────────────────────────────────────────
            if isinstance(event.get("message"), str):
                event["message"] = _scrub_string(event["message"])

            # ── Scrub event.logentry.message ──────────────────────────────────
            logentry = event.get("logentry")
            if isinstance(logentry, dict) and isinstance(logentry.get("message"), str):
                logentry["message"] = _scrub_string(logentry["message"])

            # ── Scrub event.extra values ──────────────────────────────────────
            extra = event.get("extra")
            if isinstance(extra, dict):
                _scrub_dict_values(extra)

            # ── Scrub event.request.data (POST body captured by FastAPI) ─────
            request_data = event.get("request", {}).get("data")
            if isinstance(request_data, dict):
                _scrub_dict_values(request_data)

            # ── Scrub breadcrumb messages and data ────────────────────────────
            breadcrumbs = event.get("breadcrumbs", {})
            if isinstance(breadcrumbs, dict):
                for crumb in breadcrumbs.get("values", []):
                    if not isinstance(crumb, dict):
                        continue
                    if isinstance(crumb.get("message"), str):
                        crumb["message"] = _scrub_string(crumb["message"])
                    crumb_data = crumb.get("data")
                    if isinstance(crumb_data, dict):
                        _scrub_dict_values(crumb_data)

        except Exception:  # noqa: BLE001
            # A buggy scrub must NEVER drop events — log and return original.
            logging.getLogger("sentry.before_send").exception(
                "before_send scrub hook raised; returning event unmodified"
            )

        return event

    return _scrub_and_tag


def init_sentry(component: str) -> bool:
    """Initialize Sentry for *component* ("web" or "inbox_worker").

    Returns True if Sentry was initialized, False if DSN is not configured
    (no-op path — safe to call in any environment).
    """
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        _log.info("sentry.disabled", reason="SENTRY_DSN not set", component=component)
        return False

    environment = os.getenv("SENTRY_ENVIRONMENT", "production")
    release = os.getenv("SENTRY_RELEASE") or None  # None → Sentry auto-detects from git
    traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))

    # Build integrations list — FastAPI/Starlette only for the web service.
    integrations = [
        AsyncioIntegration(),
        AsyncPGIntegration(),
        HttpxIntegration(),
        LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
    ]
    if component == "web":
        integrations = [
            FastApiIntegration(transaction_style="endpoint"),
            StarletteIntegration(transaction_style="endpoint"),
        ] + integrations

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        traces_sample_rate=traces_sample_rate,
        profiles_sample_rate=0.0,       # profiling off — budget conscious
        send_default_pii=False,         # PII scrubbing done in before_send
        attach_stacktrace=True,
        max_breadcrumbs=50,
        integrations=integrations,
        before_send=_make_before_send(component),
    )

    sentry_sdk.set_tag("component", component)

    _log.info(
        "sentry.initialized",
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        component=component,
    )
    return True
