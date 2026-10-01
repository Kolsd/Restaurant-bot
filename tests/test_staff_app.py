"""
tests/test_staff_app.py — Unified Staff App (/staff).

Covers:
  1. app.services.staff_sections.sections_for_roles — the single source of
     truth for role -> visible sections (waiter-only, admin, multi-role).
  2. GET /api/staff/sections — the real, server-enforced session gate for
     the Staff App (the /staff HTML shell itself is served unconditionally,
     same as every operational page before it — see app/routes/dashboard.py
     staff_app_page — the client-side JS in staff-shell.js does the
     token-presence redirect, exactly like the old cashier.js/waiter.js/etc.
     `if (!_token) location.href = '/login'` guard did).
  3. GET /staff renders the shell (200, references staff-shell.js).
  4. The old one-HTML-per-role URLs are gone (404, no redirect).

No live database required — everything DB-touching is mocked, matching the
pattern in tests/test_deps_org.py and tests/test_station_routing.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.staff_sections import (
    ALL_OPERATIONAL_SECTIONS,
    ALL_SECTIONS,
    default_section_for_roles,
    sections_for_roles,
)


# ══════════════════════════════════════════════════════════════════════
# 1. Role -> section mapping (pure function, no I/O)
# ══════════════════════════════════════════════════════════════════════

def test_waiter_only_sees_waiter():
    assert sections_for_roles(["mesero"]) == ["waiter"]


def test_cashier_only_sees_cashier_and_delivery():
    """Cashier roles also grant "delivery" (Domicilios, chunk 4 of the web
    delivery/pickup wave — docs/claude/delivery-web.md) — a cashier logs
    into ONE section key that shows both queues, not two separate logins."""
    assert sections_for_roles(["caja"]) == ["cashier", "delivery"]


def test_kitchen_only_sees_kitchen():
    assert sections_for_roles(["cocina"]) == ["kitchen"]


def test_bar_only_sees_bar():
    assert sections_for_roles(["bar"]) == ["bar"]


def test_courier_only_sees_courier():
    assert sections_for_roles(["domiciliario"]) == ["courier"]


def test_multi_role_caja_mesero_sees_both():
    """A user with roles.staff = ['caja', 'mesero'] must see BOTH sections."""
    sections = sections_for_roles(["caja", "mesero"])
    assert "cashier" in sections
    assert "delivery" in sections  # caja also grants Domicilios (chunk 4)
    assert "waiter" in sections
    # No section the user doesn't have a role for.
    assert "kitchen" not in sections
    assert "bar" not in sections
    assert "courier" not in sections
    # Sidebar order is stable regardless of role order.
    assert sections == ["cashier", "delivery", "waiter"]


def test_admin_roles_see_every_section():
    for role in ("owner", "admin", "gerente"):
        assert sections_for_roles([role]) == list(ALL_SECTIONS)


def test_unrecognized_role_gets_no_section():
    """`otro` (or any role with no station) gets none — the Staff App shows
    "ask an admin for a role" instead of an empty shell."""
    assert sections_for_roles(["otro"]) == []


def test_empty_roles_get_no_section():
    assert sections_for_roles([]) == []


def test_legacy_english_role_aliases_map_correctly():
    """cashier/waiter/cook/cocinero/cajero/delivery aliases used elsewhere
    in the codebase (auth_routes._ROLE_REDIRECT) must resolve the same as
    their Spanish canonical role."""
    assert sections_for_roles(["waiter"]) == ["waiter"]
    assert sections_for_roles(["cashier"]) == ["cashier", "delivery"]
    assert sections_for_roles(["cajero"]) == ["cashier", "delivery"]
    assert sections_for_roles(["cook"]) == ["kitchen"]
    assert sections_for_roles(["cocinero"]) == ["kitchen"]
    assert sections_for_roles(["delivery"]) == ["courier"]


def test_default_section_for_roles_prefers_operational_section():
    assert default_section_for_roles(["mesero"]) == "waiter"
    assert default_section_for_roles(["otro"]) is None
    assert default_section_for_roles(["owner"]) == "cashier"  # first in ALL_SECTIONS


# ══════════════════════════════════════════════════════════════════════
# 2. GET /api/staff/sections — real session gate + role wiring
# ══════════════════════════════════════════════════════════════════════

def test_staff_sections_endpoint_requires_session(client):
    """No Authorization header at all -> 401. This is the actual, enforceable
    'you need a session' gate for the Staff App (see module docstring)."""
    response = client.get("/api/staff/sections")
    assert response.status_code == 401


def test_staff_sections_endpoint_waiter_only(client, monkeypatch):
    monkeypatch.setattr(
        "app.routes.auth_routes.get_current_user",
        AsyncMock(return_value={"username": "staff:abc", "role": "mesero"}),
    )
    response = client.get("/api/staff/sections", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == ["waiter"]


def test_staff_sections_endpoint_admin_sees_all_operational_sections(client, monkeypatch):
    """Admins (users-table accounts, not a `staff` row) get every section."""
    monkeypatch.setattr(
        "app.routes.auth_routes.get_current_user",
        AsyncMock(return_value={"username": "owner@test.com", "role": "owner"}),
    )
    response = client.get("/api/staff/sections", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    assert response.json()["sections"] == list(ALL_OPERATIONAL_SECTIONS)


def test_staff_sections_endpoint_staff_gerente_sees_every_section(client, monkeypatch):
    """A `staff`-table gerente (username 'staff:<uuid>') sees every section."""
    monkeypatch.setattr(
        "app.routes.auth_routes.get_current_user",
        AsyncMock(return_value={"username": "staff:mgr-1", "role": "gerente"}),
    )
    response = client.get("/api/staff/sections", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    assert response.json()["sections"] == list(ALL_SECTIONS)


def test_staff_sections_endpoint_multi_role(client, monkeypatch):
    monkeypatch.setattr(
        "app.routes.auth_routes.get_current_user",
        AsyncMock(return_value={"username": "staff:xyz", "role": "caja,mesero"}),
    )
    response = client.get("/api/staff/sections", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    sections = response.json()["sections"]
    assert sections == ["cashier", "delivery", "waiter"]


# ══════════════════════════════════════════════════════════════════════
# 3. GET /staff — the shell itself
# ══════════════════════════════════════════════════════════════════════

def test_staff_page_renders_shell(client):
    """GET /staff serves the unified shell HTML and wires the section switcher.

    Served unconditionally (200) — like every operational page before it —
    because a plain browser navigation can never carry the localStorage
    Bearer token as a header. staff-shell.js does the client-side auth
    redirect, and /api/staff/sections is the real server-side gate (see
    test_staff_sections_endpoint_requires_session above).
    """
    response = client.get("/staff")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "staff-shell.js" in body
    assert 'id="staff-section-root"' in body


@pytest.mark.parametrize(
    "path",
    ["/waiter", "/cashier", "/kitchen", "/bar", "/courier", "/staff-hq", "/staff-clock"],
)
def test_old_role_page_urls_are_gone(client, path):
    """The old one-HTML-per-role pages must 404 — no redirect, no fallback
    (product decision: Staff App unification, 2026-09-14, no customers yet)."""
    response = client.get(path)
    assert response.status_code == 404
