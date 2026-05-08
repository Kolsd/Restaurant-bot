"""
tests/test_audit_log.py

Unit tests for the HQ audit log infrastructure:
  - db_log_audit_event / db_get_audit_log / db_count_audit_log
  - AuditMiddleware behaviour (fires on POST/PATCH/DELETE to /api/internal/*,
    ignores GET and non-internal paths, survives a DB write failure)

All tests use mocked DB pools — no live database required.
"""

from __future__ import annotations

import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_pool(fetchval=None, fetch_rows=None):
    """Build a minimal asyncpg pool mock."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetch    = AsyncMock(return_value=fetch_rows or [])
    conn.execute  = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__  = AsyncMock(return_value=False)
    return pool, conn


def _fake_asyncpg_row(**kwargs):
    """Return an object that behaves like an asyncpg Record for dict(row) and row.items()."""
    data = dict(kwargs)
    m = MagicMock()
    m.items = MagicMock(return_value=data.items())
    m.__iter__ = MagicMock(return_value=iter(data.items()))
    # Support dict(row) via keys/values protocol
    m.keys   = MagicMock(return_value=data.keys())
    m.values = MagicMock(return_value=data.values())
    return m


# ═══════════════════════════════════════════════════════════════════════════════
# Repo: db_log_audit_event
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_log_audit_event_success():
    """db_log_audit_event inserts a row and returns its id."""
    pool, conn = _make_pool(fetchval=42)

    with patch(
        "app.repositories.internal.audit_log_repo._get_pool",
        AsyncMock(return_value=pool),
    ):
        from app.repositories.internal.audit_log_repo import db_log_audit_event
        row_id = await db_log_audit_event(
            actor="superadmin",
            action="org.updated",
            target_type="organization",
            target_id="7",
            org_id=7,
            payload={"method": "PATCH", "status_code": 200},
            request_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert row_id == 42
    conn.fetchval.assert_awaited_once()
    call_sql = conn.fetchval.call_args[0][0]
    assert "INSERT INTO hq_audit_log" in call_sql


@pytest.mark.asyncio
async def test_log_audit_event_db_error_returns_zero():
    """db_log_audit_event is best-effort: DB errors return 0, never raise."""
    pool, conn = _make_pool()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch(
        "app.repositories.internal.audit_log_repo._get_pool",
        AsyncMock(return_value=pool),
    ):
        from app.repositories.internal.audit_log_repo import db_log_audit_event
        result = await db_log_audit_event(
            actor="superadmin",
            action="org.deleted",
        )

    assert result == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Repo: db_get_audit_log — filters + sort direction
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_audit_log_sql_contains_order_and_limit():
    """db_get_audit_log issues ORDER BY created_at DESC and a LIMIT clause."""
    pool, conn = _make_pool(fetch_rows=[])

    with patch(
        "app.repositories.internal.audit_log_repo._get_pool",
        AsyncMock(return_value=pool),
    ):
        from app.repositories.internal.audit_log_repo import db_get_audit_log
        await db_get_audit_log(limit=25)

    sql = conn.fetch.call_args[0][0]
    assert "ORDER BY created_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_get_audit_log_org_filter():
    """db_get_audit_log with org_id adds org_id = $N to SQL and passes value."""
    pool, conn = _make_pool(fetch_rows=[])

    with patch(
        "app.repositories.internal.audit_log_repo._get_pool",
        AsyncMock(return_value=pool),
    ):
        from app.repositories.internal.audit_log_repo import db_get_audit_log
        await db_get_audit_log(org_id=99)

    sql, *params = conn.fetch.call_args[0]
    assert "org_id = $" in sql
    assert 99 in params


@pytest.mark.asyncio
async def test_count_audit_log():
    """db_count_audit_log returns integer from fetchval."""
    pool, conn = _make_pool(fetchval=17)

    with patch(
        "app.repositories.internal.audit_log_repo._get_pool",
        AsyncMock(return_value=pool),
    ):
        from app.repositories.internal.audit_log_repo import db_count_audit_log
        count = await db_count_audit_log(org_id=5)

    assert count == 17


# ═══════════════════════════════════════════════════════════════════════════════
# Middleware: AuditMiddleware behaviour
# ═══════════════════════════════════════════════════════════════════════════════

def _make_request(method: str, path: str) -> MagicMock:
    req = MagicMock()
    req.method = method
    req.url = MagicMock()
    req.url.path = path
    req.headers = {"Authorization": "Bearer tok", "user-agent": "pytest/1.0"}
    req.client = MagicMock(host="127.0.0.1")
    req.cookies = {}
    return req


def _make_response(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    return resp


@pytest.mark.asyncio
async def test_middleware_fires_on_internal_post():
    """Middleware calls db_log_audit_event for POST /api/internal/* (2xx response)."""
    from app.services.audit_middleware import AuditMiddleware

    request  = _make_request("POST", "/api/internal/admin/organizations")
    response = _make_response(200)

    mw = AuditMiddleware(app=MagicMock())

    with patch(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        AsyncMock(return_value=1),
    ) as mock_log, patch(
        "app.services.audit_middleware.bypass_tenant_scope",
    ) as mock_bypass:
        mock_bypass.return_value.__enter__ = MagicMock(return_value=None)
        mock_bypass.return_value.__exit__  = MagicMock(return_value=False)
        await mw._maybe_log(request, response, duration_ms=42)

    mock_log.assert_awaited_once()
    kwargs = mock_log.call_args[1]
    assert kwargs["action"] == "org.created"
    assert kwargs["request_ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_middleware_ignores_get():
    """Middleware does NOT log GET requests (read-only, no state change)."""
    from app.services.audit_middleware import AuditMiddleware

    mock_log = AsyncMock(return_value=1)
    request  = _make_request("GET", "/api/internal/admin/organizations")
    response = _make_response(200)

    mw = AuditMiddleware(app=MagicMock())

    with patch(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        mock_log,
    ):
        await mw._maybe_log(request, response, duration_ms=5)

    mock_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_ignores_non_internal_path():
    """Middleware does NOT log calls outside /api/internal/."""
    from app.services.audit_middleware import AuditMiddleware

    mock_log = AsyncMock(return_value=1)
    request  = _make_request("POST", "/api/orders/123/confirm")
    response = _make_response(200)

    mw = AuditMiddleware(app=MagicMock())

    with patch(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        mock_log,
    ):
        await mw._maybe_log(request, response, duration_ms=5)

    mock_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_ignores_4xx_responses():
    """Middleware does NOT log failed (4xx) responses — action didn't take effect."""
    from app.services.audit_middleware import AuditMiddleware

    mock_log = AsyncMock(return_value=1)
    request  = _make_request("POST", "/api/internal/admin/organizations")
    response = _make_response(422)

    mw = AuditMiddleware(app=MagicMock())

    with patch(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        mock_log,
    ):
        await mw._maybe_log(request, response, duration_ms=5)

    mock_log.assert_not_awaited()


@pytest.mark.asyncio
async def test_middleware_survives_db_write_failure():
    """dispatch() never raises to the caller even when the audit repo explodes.

    _maybe_log itself may raise; the outer dispatch() catches it via try/except
    and logs via structlog. The original response must be returned intact.
    """
    from app.services.audit_middleware import AuditMiddleware

    fake_response = _make_response(200)

    async def call_next(req):
        return fake_response

    mw = AuditMiddleware(app=MagicMock())
    request = _make_request("DELETE", "/api/internal/admin/organizations/7")

    with patch(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        AsyncMock(side_effect=RuntimeError("DB down")),
    ), patch(
        "app.services.audit_middleware.bypass_tenant_scope",
    ) as mock_bypass:
        mock_bypass.return_value.__enter__ = MagicMock(return_value=None)
        mock_bypass.return_value.__exit__  = MagicMock(return_value=False)

        # dispatch() must NOT raise — it catches audit errors internally
        result = await mw.dispatch(request, call_next)

    assert result is fake_response


@pytest.mark.asyncio
async def test_middleware_dispatch_never_breaks_request():
    """Full dispatch() path: audit errors must not propagate to the caller."""
    from app.services.audit_middleware import AuditMiddleware

    fake_response = _make_response(200)

    async def call_next(req):
        return fake_response

    mw = AuditMiddleware(app=MagicMock())
    request = _make_request("PATCH", "/api/internal/admin/organizations/3")

    with patch.object(mw, "_maybe_log", AsyncMock(side_effect=Exception("boom"))):
        result = await mw.dispatch(request, call_next)

    assert result is fake_response


# ═══════════════════════════════════════════════════════════════════════════════
# Action + target derivation helpers
# ═══════════════════════════════════════════════════════════════════════════════

def test_derive_action_known_paths():
    from app.services.audit_middleware import _derive_action
    assert _derive_action("POST",   "/api/internal/admin/organizations")     == "org.created"
    assert _derive_action("PATCH",  "/api/internal/admin/organizations/5")   == "org.updated"
    assert _derive_action("DELETE", "/api/internal/admin/organizations/5")   == "org.deleted"
    assert _derive_action("POST",   "/api/internal/admin/create-user")       == "user.created"
    assert _derive_action("POST",   "/api/internal/crm/prospects/3/convert") == "prospect.converted"
    assert _derive_action("POST",   "/api/internal/admin/login")             == "admin.login"


def test_derive_action_generic_fallback():
    from app.services.audit_middleware import _derive_action
    action = _derive_action("POST", "/api/internal/crm/new-unknown-endpoint")
    assert "post" in action.lower()


def test_derive_target_extracts_id():
    from app.services.audit_middleware import _derive_target
    ttype, tid = _derive_target("/api/internal/admin/organizations/42")
    assert tid == "42"
    assert ttype == "organization"


def test_derive_target_no_id():
    from app.services.audit_middleware import _derive_target
    ttype, tid = _derive_target("/api/internal/admin/organizations")
    assert tid is None
    assert ttype == "organization"
