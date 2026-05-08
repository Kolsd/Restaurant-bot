"""
Audit middleware — records every state-changing /api/internal/* call.

Captures: actor, action (derived from method+path), target_type/id,
request metadata (ip, user-agent), response status, duration.

Best-effort: if audit logging fails the request is never blocked.
"""

from __future__ import annotations

import re
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

# ── Action name mapping ──────────────────────────────────────────────────────
# Maps (METHOD, path-pattern) → action name.
# Checked in order; first match wins. Patterns use regex with named groups.

_ACTION_RULES: list[tuple[str, re.Pattern, str]] = [
    # Organizations
    ("POST",   re.compile(r"^/api/internal/admin/organizations$"),                         "org.created"),
    ("PATCH",  re.compile(r"^/api/internal/admin/organizations/\d+$"),                     "org.updated"),
    ("DELETE", re.compile(r"^/api/internal/admin/organizations/\d+$"),                     "org.deleted"),
    # Locations
    ("POST",   re.compile(r"^/api/internal/admin/organizations/\d+/locations$"),           "location.created"),
    ("PATCH",  re.compile(r"^/api/internal/admin/locations/\d+$"),                         "location.updated"),
    ("DELETE", re.compile(r"^/api/internal/admin/locations/\d+$"),                         "location.deleted"),
    # Users
    ("POST",   re.compile(r"^/api/internal/admin/create-user$"),                           "user.created"),
    ("POST",   re.compile(r"^/api/internal/admin/delete-user$"),                           "user.deleted"),
    # Subscriptions
    ("POST",   re.compile(r"^/api/internal/admin/set-subscription$"),                      "subscription.updated"),
    ("POST",   re.compile(r"^/api/internal/billing/[^/]+"),                                "billing.updated"),
    # Restaurants (legacy endpoints)
    ("POST",   re.compile(r"^/api/internal/admin/create-restaurant$"),                     "org.created"),
    ("POST",   re.compile(r"^/api/internal/admin/update-restaurant$"),                     "org.updated"),
    # CRM prospects
    ("POST",   re.compile(r"^/api/internal/crm/prospects/\d+/convert"),                    "prospect.converted"),
    ("POST",   re.compile(r"^/api/internal/crm/prospects$"),                               "prospect.created"),
    ("PATCH",  re.compile(r"^/api/internal/crm/prospects/\d+$"),                           "prospect.updated"),
    ("DELETE", re.compile(r"^/api/internal/crm/prospects/\d+$"),                           "prospect.deleted"),
    ("POST",   re.compile(r"^/api/internal/crm/prospects/\d+/stage$"),                     "prospect.stage_moved"),
    ("POST",   re.compile(r"^/api/internal/crm/send$"),                                    "crm.message_sent"),
    # Auth (login/logout are state-changing but low-value; log them)
    ("POST",   re.compile(r"^/api/internal/admin/login$"),                                 "admin.login"),
    ("POST",   re.compile(r"^/api/internal/admin/logout$"),                                "admin.logout"),
]

# Regex to extract a numeric or slug ID from path segments
_ID_RE = re.compile(r"(?<=/)([\w-]+)$|(?<=/)([\w-]+)(?=/)")

# Map path-segment prefix → target_type
_TARGET_TYPE_MAP = {
    "organizations": "organization",
    "locations":     "location",
    "users":         "user",
    "prospects":     "prospect",
    "billing":       "subscription",
    "restaurants":   "organization",
}


def _derive_action(method: str, path: str) -> str:
    """Return a dot-namespaced action name for the given (method, path) pair."""
    for rule_method, pattern, action in _ACTION_RULES:
        if rule_method == method and pattern.search(path):
            return action
    # Generic fallback: <last-meaningful-segment>.<method.lower()>
    parts = [p for p in path.rstrip("/").split("/") if p and not p.isdigit()]
    resource = parts[-1] if parts else "internal"
    return f"{resource}.{method.lower()}"


def _derive_target(path: str) -> tuple[Optional[str], Optional[str]]:
    """Return (target_type, target_id) derived from path segments."""
    segments = [p for p in path.strip("/").split("/") if p]
    # Find the last numeric segment as ID
    target_id: Optional[str] = None
    for seg in reversed(segments):
        if seg.isdigit():
            target_id = seg
            break

    # Find target_type from known segments
    target_type: Optional[str] = None
    for seg in reversed(segments):
        if seg in _TARGET_TYPE_MAP:
            target_type = _TARGET_TYPE_MAP[seg]
            break

    return target_type, target_id


def _extract_actor(request: Request) -> str:
    """Best-effort actor identification from Authorization header or cookie."""
    # Authorization: Bearer <token>
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        # We have a token but don't resolve it to email here (async not available).
        # The session identity is 'superadmin' for all valid internal sessions.
        # If we ever add per-user admin accounts, resolve here.
        return "superadmin"
    # Fallback: cookie-based session (key used by the superadmin UI)
    cookie_token = request.cookies.get("admin_session", "")
    if cookie_token:
        return "superadmin"
    return "superadmin"  # internal routes are always superadmin-gated


class AuditMiddleware(BaseHTTPMiddleware):
    """Records state-changing /api/internal/* calls to hq_audit_log.

    Only fires for POST/PATCH/PUT/DELETE on /api/internal/* paths when
    the response status is < 400 (success only — failures are not audited
    since the action didn't take effect).
    """

    _WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            await self._maybe_log(request, response, duration_ms)
        except Exception:
            log.exception("audit_middleware.log_failed",
                          path=request.url.path,
                          method=request.method)

        return response

    async def _maybe_log(
        self,
        request: Request,
        response: Response,
        duration_ms: int,
    ) -> None:
        path = request.url.path
        method = request.method.upper()

        # Only instrument state-changing calls to internal API
        if not path.startswith("/api/internal/"):
            return
        if method not in self._WRITE_METHODS:
            return
        if response.status_code >= 400:
            return

        actor = _extract_actor(request)
        action = _derive_action(method, path)
        target_type, target_id = _derive_target(path)

        payload = {
            "path": path,
            "method": method,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        }

        request_ip = (request.client.host if request.client else None)
        user_agent = request.headers.get("user-agent")

        # Import here to avoid circular import at module load time
        from app.repositories.internal.audit_log_repo import db_log_audit_event  # noqa: PLC0415

        with bypass_tenant_scope("audit_middleware_global_write"):
            await db_log_audit_event(
                actor=actor,
                action=action,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                request_ip=request_ip,
                user_agent=user_agent,
            )
