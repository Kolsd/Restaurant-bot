"""
tests/test_delivery_eta_endpoint.py
====================================
Validation-layer tests for POST /api/delivery/orders/{order_id}/eta.

Uses the shared `client` TestClient fixture from conftest.py — same pattern
as test_orders_flow.py — so the FastAPI lifespan starts ONCE for the entire
test session. Auth helpers are monkeypatched per-test to avoid hitting the
real DB pool (which can be in an inconsistent state when many TestClient
calls precede us).

Coverage:
  - Body validation: minutes must be 1..180 (returns 422 for 0 / 181 / -1).
  - Auth gate: anonymous calls (no Authorization header) → 401/403.

DB-touching cases (set/get/mark + tenant isolation) live in test_delivery_eta.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from tests.conftest import patch_auth


_HEADERS = {"Authorization": "Bearer tok"}


def test_eta_endpoint_anonymous_call_rejected(client, monkeypatch):
    """No Authorization header → 401, never reaches handler.

    We stub verify_token to return None for empty tokens — same as production
    behaviour but without the DB lookup. This keeps the test from depending on
    the live test pool being healthy when many other tests run before us.
    """
    monkeypatch.setattr(
        "app.routes.deps.verify_token",
        AsyncMock(return_value=None),
    )

    resp = client.post(
        "/api/delivery/orders/anything/eta",
        json={"minutes": 30},
    )
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 for unauthenticated call, got {resp.status_code}: {resp.text}"
    )


def test_eta_endpoint_rejects_zero(client, monkeypatch):
    """minutes=0 → 422."""
    patch_auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))

    resp = client.post(
        "/api/delivery/orders/anything/eta",
        json={"minutes": 0},
        headers=_HEADERS,
    )
    assert resp.status_code == 422, (
        f"Expected 422 for minutes=0, got {resp.status_code}: {resp.text}"
    )


def test_eta_endpoint_rejects_above_180(client, monkeypatch):
    """minutes=181 → 422."""
    patch_auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))

    resp = client.post(
        "/api/delivery/orders/anything/eta",
        json={"minutes": 181},
        headers=_HEADERS,
    )
    assert resp.status_code == 422, (
        f"Expected 422 for minutes=181, got {resp.status_code}: {resp.text}"
    )


def test_eta_endpoint_rejects_negative(client, monkeypatch):
    """minutes=-1 → 422."""
    patch_auth(monkeypatch)
    import app.services.database as db_mod
    monkeypatch.setattr(db_mod, "db_check_module", AsyncMock(return_value=True))

    resp = client.post(
        "/api/delivery/orders/anything/eta",
        json={"minutes": -1},
        headers=_HEADERS,
    )
    assert resp.status_code == 422, (
        f"Expected 422 for minutes=-1, got {resp.status_code}: {resp.text}"
    )
