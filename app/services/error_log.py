"""
Mesio HQ error log — record a failure with the restaurant and sede it hit.

`record_error(...)` is fire-and-forget: it schedules the insert and returns.
Recording can never break, slow down or change the request that failed —
the original exception keeps propagating, the diner still gets the fallback.

Sources: "http" (an unhandled exception or a 5xx response, from the
middleware in main.py) and "bot" (an LLM turn that failed and fell back to
"tengo un problema técnico", from agent.py).

Messages are trimmed and phone-like digit runs masked before storing: the
log is for diagnosing, not a second copy of customer data.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import re

import asyncpg

from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)

_MAX_MESSAGE = 500
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{7,}\d")
_DIGITS_RE = re.compile(r"\d+")
_pending: set[asyncio.Task] = set()


def clean_message(message: str | None) -> str:
    text = (message or "").strip().replace("\n", " ")
    text = _PHONE_RE.sub("***", text)
    return text[:_MAX_MESSAGE]


def fingerprint(source: str, route: str | None, error_type: str, message: str | None) -> str:
    """Same failure → same fingerprint: ids, counts and amounts in the
    message (and in the path) are stripped so repeats group together."""
    shape = "|".join([
        source,
        _DIGITS_RE.sub("#", route or ""),
        error_type,
        _DIGITS_RE.sub("#", (message or "").split(":")[0])[:120],
    ])
    return hashlib.sha1(shape.encode("utf-8")).hexdigest()[:16]  # noqa: S324 — grouping key, not security


async def _write(row: dict) -> None:
    from app.repositories.internal import errors_repo  # noqa: PLC0415
    try:
        with bypass_tenant_scope("error_log: Mesio-internal platform_errors insert"):
            await errors_repo.db_insert_error(**row)
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as exc:
        log.warning("error_log.write_failed", error=type(exc).__name__, source=row.get("source"))


def record_error(*, source: str, error_type: str, message: str | None = None,
                 org_id: int | None = None, location_id: int | None = None,
                 route: str | None = None, method: str | None = None,
                 status: int | None = None, request_id: str | None = None) -> None:
    msg = clean_message(message)
    row = {
        "source": source,
        "org_id": int(org_id) if org_id else None,
        "location_id": int(location_id) if location_id else None,
        "route": (route or "")[:200] or None,
        "method": method,
        "status": status,
        "error_type": (error_type or "Error")[:120],
        "message": msg or None,
        "fingerprint": fingerprint(source, route, error_type, msg),
        "request_id": request_id,
    }
    try:
        # A clean context: the failing request may be inside tenant_scope,
        # and bypass_tenant_scope refuses to nest inside one.
        task = asyncio.get_running_loop().create_task(_write(row), context=contextvars.Context())
    except RuntimeError:  # no running loop (sync context): nothing to schedule on
        log.warning("error_log.no_loop", source=source)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for scheduled writes (tests, shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
