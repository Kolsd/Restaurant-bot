"""
tests/test_internal_search.py

Tests for the Mesio HQ global search endpoint.

Uses the shared TestClient from conftest (no real DB) with mocked session auth
and mocked get_pool. The `mock_superadmin_session` fixture ensures any non-empty
Bearer token passes `verify_superadmin`. The `mock_pool_search` fixture
intercepts `app.routes.internal.search.get_pool`.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone


FAKE_TOKEN = "superadmin-test-token-abc"


def _auth():
    return {"Authorization": f"Bearer {FAKE_TOKEN}"}


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def with_auth(mock_superadmin_session):
    """Activates superadmin session mock (from conftest)."""
    yield


def _empty_pool():
    """Pool mock that always returns empty rows for both queries."""
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(mock_conn))
    return pool, mock_conn


# ── 1. Empty query → returns quick actions only ───────────────────────────────

def test_search_empty_query_returns_actions(client, with_auth):
    """q='' → only quick actions, DB is NOT queried."""
    with patch(
        "app.routes.internal.search.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        resp = client.get("/api/internal/search", headers=_auth())

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "results" in data
    results = data["results"]
    assert len(results) >= 1
    types = {r["type"] for r in results}
    assert types == {"action"}, f"Expected only actions, got: {types}"
    # DB not hit for empty query
    mock_get_pool.assert_not_called()


def test_search_one_char_returns_actions_only(client, with_auth):
    """q with 1 char → only actions (below 2-char threshold), no DB."""
    with patch(
        "app.routes.internal.search.get_pool",
        new_callable=AsyncMock,
    ) as mock_get_pool:
        resp = client.get("/api/internal/search?q=a", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    mock_get_pool.assert_not_called()
    for r in data["results"]:
        assert r["type"] == "action"


# ── 2. Action filtering ───────────────────────────────────────────────────────

def test_search_action_filter_crm(client, with_auth):
    """q='crm' → pipeline CRM action is returned."""
    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=_empty_pool()[0]):
        resp = client.get("/api/internal/search?q=crm", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    titles = [r["title"] for r in data["results"]]
    assert any("CRM" in t or "crm" in t.lower() for t in titles), \
        f"CRM action not found in: {titles}"


def test_search_action_filter_costos(client, with_auth):
    """q='costo' → Ver costos action is returned."""
    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=_empty_pool()[0]):
        resp = client.get("/api/internal/search?q=costo", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    titles = [r["title"] for r in data["results"]]
    assert any("costo" in t.lower() for t in titles), \
        f"Costos action not found in: {titles}"


def test_search_unknown_query_returns_empty_actions(client, with_auth):
    """q='zzz' (2+ chars, no matching actions, no DB rows) → empty list."""
    pool, _ = _empty_pool()
    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=pool):
        resp = client.get("/api/internal/search?q=zzz", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert isinstance(data["results"], list)
    # No action should match 'zzz', no DB rows → empty
    assert data["results"] == []


# ── 3. Query >= 2 chars → DB is queried ──────────────────────────────────────

def test_search_query_hits_db(client, with_auth):
    """q='ma' → get_pool is called; fetch() called twice (tenants + prospects)."""
    pool, mock_conn = _empty_pool()
    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=pool):
        resp = client.get("/api/internal/search?q=ma", headers=_auth())

    assert resp.status_code == 200
    # Fetch was called twice: once for organizations, once for prospects
    assert mock_conn.fetch.call_count == 2


def test_search_returns_tenant_results(client, with_auth):
    """q='test' → tenant row from DB appears in results with correct shape."""
    mock_tenant_row = _make_asyncpg_row({
        "id": 42,
        "name": "Restaurante Test",
        "plan_code": "restaurante",
        "whatsapp_number": "+573001234567",
        "last_order_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    })
    mock_conn = AsyncMock()

    async def _fetch_side_effect(query, *args):
        if "organizations" in query:
            return [mock_tenant_row]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch_side_effect)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(mock_conn))

    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=pool):
        resp = client.get("/api/internal/search?q=test", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    tenant_results = [r for r in data["results"] if r["type"] == "tenant"]
    assert len(tenant_results) == 1
    r = tenant_results[0]
    assert r["id"] == 42
    assert r["title"] == "Restaurante Test"
    assert "restaurante" in r["subtitle"].lower()
    assert r["url"] == "/internal/superadmin#org=42"


def test_search_returns_prospect_results(client, with_auth):
    """q='carlos' → prospect row from DB appears in results with correct shape."""
    mock_prospect_row = _make_asyncpg_row({
        "id": 5,
        "owner_name": "Carlos Pérez",
        "phone": "+573009876543",
        "restaurant_name": "La Parrilla de Carlos",
        "stage": "demo",
        "city": "Bogotá",
        "last_contact_at": datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc),
    })
    mock_conn = AsyncMock()

    async def _fetch_side_effect(query, *args):
        if "prospects" in query:
            return [mock_prospect_row]
        return []

    mock_conn.fetch = AsyncMock(side_effect=_fetch_side_effect)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(mock_conn))

    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=pool):
        resp = client.get("/api/internal/search?q=carlos", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    prospect_results = [r for r in data["results"] if r["type"] == "prospect"]
    assert len(prospect_results) == 1
    r = prospect_results[0]
    assert r["id"] == 5
    assert r["title"] == "Carlos Pérez"
    assert "demo" in r["subtitle"].lower()
    assert "Bogotá" in r["subtitle"]
    assert r["url"] == "/internal/crm#prospect=5"


# ── 4. Auth guard ─────────────────────────────────────────────────────────────

def test_search_requires_auth(client):
    """No auth header → 401."""
    resp = client.get("/api/internal/search")
    assert resp.status_code == 401


def test_search_bad_token_rejected(client):
    """Invalid token → 401 or 403 (session lookup returns None)."""
    from unittest.mock import patch as _patch, AsyncMock as _AM
    # Mock session store to return None for an unknown token (not superadmin)
    async def _no_session(token):
        return None

    with _patch("app.repositories.sessions_repo.get_session", _AM(side_effect=_no_session)):
        resp = client.get(
            "/api/internal/search",
            headers={"Authorization": "Bearer totally-invalid-token"}
        )
    assert resp.status_code in (401, 403)


# ── 5. Result cap ─────────────────────────────────────────────────────────────

def test_search_results_capped_at_20(client, with_auth):
    """Total results never exceed 20 even if actions + DB rows add up to more."""
    mock_conn = AsyncMock()

    async def _fetch_side_effect(query, *args):
        if "organizations" in query:
            return [_make_asyncpg_row({
                "id": i, "name": f"Restaurante {i}", "plan_code": "pulso",
                "whatsapp_number": None, "last_order_at": None,
            }) for i in range(15)]
        return [_make_asyncpg_row({
            "id": i, "owner_name": f"Owner {i}", "phone": f"+57300{i:05d}",
            "restaurant_name": f"Rest {i}", "stage": "nuevo",
            "city": None, "last_contact_at": None,
        }) for i in range(15)]

    mock_conn.fetch = AsyncMock(side_effect=_fetch_side_effect)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxMgr(mock_conn))

    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock,
               return_value=pool):
        resp = client.get("/api/internal/search?q=re", headers=_auth())

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) <= 20


# ── 6. Result shape ───────────────────────────────────────────────────────────

def test_actions_have_required_fields(client, with_auth):
    """Every action in the result has type, title, subtitle, url."""
    with patch("app.routes.internal.search.get_pool", new_callable=AsyncMock):
        resp = client.get("/api/internal/search", headers=_auth())

    data = resp.json()
    for r in data["results"]:
        assert "type" in r
        assert "title" in r
        assert "subtitle" in r
        assert "url" in r
        assert r["type"] == "action"
        assert r["url"].startswith("/internal")


# ── Helpers ───────────────────────────────────────────────────────────────────

class _AsyncCtxMgr:
    """Minimal async context manager that yields a value."""
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


def _make_asyncpg_row(d: dict):
    """Create an object that behaves like an asyncpg Record for dict-style access."""
    row = MagicMock()
    row.__getitem__ = lambda self, k: d[k]
    row.get = lambda k, default=None: d.get(k, default)
    row.keys = lambda: d.keys()
    return row
