"""
tests/test_delivery_ordering_page.py
=====================================
Chunk 5 of the delivery/pickup web wave (docs/claude/delivery-web.md):
the customer ordering page `/pedir/{slug}`. This file covers only the
NEW server-side surface added for that page — the chat itself is already
covered by tests/test_diner_routes.py (dine-in) and
tests/test_delivery_entry.py / tests/test_delivery_checkout.py
(delivery/pickup session + checkout), all of which must stay green
unmodified (the ordering page reuses that exact backend, unchanged).

Covers:
  A. GET /pedir/{slug} — serves the page for a real slug, 404s cleanly for
     an unknown one, following the SAME "org must exist" convention chosen
     for this route (see app/routes/dashboard.py::diner_delivery_entry_page).
  B. GET /api/diner/org/{slug} — turnstile_site_key appears only when
     TURNSTILE_SITE_KEY is configured; TURNSTILE_SECRET never appears in
     ANY response body regardless of configuration.
  C. Content-Security-Policy — challenges.cloudflare.com is present in both
     script-src and frame-src on every response (Turnstile loads a script
     AND renders inside an iframe).
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DB_URL, reason="TEST_DATABASE_URL not set — integration tests skipped",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset_pool() -> None:
    from app.services import database as _db_mod
    _db_mod._pool = None


def _get(client, url, **kwargs):
    _reset_pool()
    return client.get(url, **kwargs)


async def _seed_delivery_org() -> dict:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        suffix = uuid.uuid4().hex[:10]
        org_id = await conn.fetchval(
            "INSERT INTO organizations (name, slug, features) VALUES ($1, $2, $3::jsonb) RETURNING id",
            f"Pedir Page Org {suffix}", f"pedir-page-{suffix}",
            json.dumps({"currency": "COP"}),
        )
        location_id = await conn.fetchval(
            """
            INSERT INTO locations
                (org_id, name, latitude, longitude, phone, address, delivery_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            RETURNING id
            """,
            org_id, f"Sede {suffix}", 4.6097, -74.0817,
            "3011234567", "Cra 1 # 2-3",
            json.dumps({
                "delivery_enabled": True, "pickup_enabled": True, "radius_km": 50,
                "payment_methods": ["efectivo", "nequi"],
            }),
        )
        slug = await conn.fetchval("SELECT slug FROM organizations WHERE id = $1", org_id)
        return {"org_id": org_id, "location_id": location_id, "slug": slug, "suffix": suffix}
    finally:
        await conn.close()


async def _teardown_org(org_id: int) -> None:
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await conn.execute("SELECT set_config('app.org_id', $1::text, false)", str(org_id))
        await conn.execute("DELETE FROM diner_sessions WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM locations WHERE org_id = $1", org_id)
        await conn.execute("DELETE FROM organizations WHERE id = $1", org_id)
    finally:
        await conn.close()


@pytest.fixture
def pedir_org():
    info = _run(_seed_delivery_org())
    try:
        yield info
    finally:
        _run(_teardown_org(info["org_id"]))


# ── A. GET /pedir/{slug} ─────────────────────────────────────────────────


def test_pedir_page_serves_the_shared_chat_shell_for_a_real_slug(client, pedir_org):
    resp = _get(client, f"/pedir/{pedir_org['slug']}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    # It's the SAME diner-chat.html the dine-in /chat/{table_id} route
    # serves (docs/claude/delivery-web.md chunk 5: "REUSE the existing
    # diner chat code... do not fork diner-chat.html") — assert on markup
    # that only exists because this IS that shared shell, not a second copy.
    body = resp.text
    assert 'id="pedir-entry"' in body
    assert 'id="diner-composer"' in body
    assert 'diner-session.js' in body
    assert 'pages/diner-chat.js' in body


def test_pedir_page_404s_cleanly_for_an_unknown_slug(client):
    resp = _get(client, "/pedir/no-existe-este-restaurante-xyz")
    assert resp.status_code == 404


# ── B. Turnstile site key exposure ───────────────────────────────────────


def test_org_info_exposes_no_site_key_when_turnstile_unset(client, pedir_org, monkeypatch):
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SECRET", raising=False)
    resp = _get(client, f"/api/diner/org/{pedir_org['slug']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["turnstile_site_key"] is None


def test_org_info_exposes_site_key_when_configured_never_the_secret(client, pedir_org, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
    monkeypatch.setenv("TURNSTILE_SECRET", "2x00000000000000000000BB-super-secret")
    resp = _get(client, f"/api/diner/org/{pedir_org['slug']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["turnstile_site_key"] == "1x00000000000000000000AA"
    # The SECRET must never leak into this or any other response body.
    assert "2x00000000000000000000BB-super-secret" not in resp.text


def test_turnstile_secret_never_in_any_diner_response_body(client, pedir_org, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "1x00000000000000000000AA")
    monkeypatch.setenv("TURNSTILE_SECRET", "2x00000000000000000000BB-super-secret")
    for url in (f"/pedir/{pedir_org['slug']}", f"/api/diner/org/{pedir_org['slug']}"):
        resp = _get(client, url)
        assert "2x00000000000000000000BB-super-secret" not in resp.text
        assert "TURNSTILE_SECRET" not in resp.text


# ── C. CSP — Turnstile origin in script-src AND frame-src ───────────────


def test_csp_allows_turnstile_script_and_frame(client, pedir_org):
    resp = _get(client, f"/pedir/{pedir_org['slug']}")
    csp = resp.headers.get("content-security-policy", "")
    assert csp, "CSP header missing"
    directives = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, rest = part.partition(" ")
        directives[name] = rest

    assert "https://challenges.cloudflare.com" in directives.get("script-src", ""), csp
    assert "https://challenges.cloudflare.com" in directives.get("frame-src", ""), csp


def test_permissions_policy_allows_self_geolocation(client, pedir_org):
    """Regression guard: an empty `geolocation=()` allowlist silently blocks
    navigator.geolocation.getCurrentPosition() on this very page in every
    browser that enforces Permissions-Policy — found while wiring the GPS
    step of this chunk. Third-party embeds must still be denied."""
    resp = _get(client, f"/pedir/{pedir_org['slug']}")
    policy = resp.headers.get("permissions-policy", "")
    assert "geolocation=(self)" in policy, policy
