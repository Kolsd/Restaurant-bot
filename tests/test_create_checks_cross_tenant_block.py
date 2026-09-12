"""
tests/test_create_checks_cross_tenant_block.py

Regression suite for the create_checks cross-tenant write vulnerability.

Before the fix, `db_get_order_ticket_data` never returned `restaurant_id`,
so the ownership check short-circuited and ANY authenticated user could
split ANY ticket across tenants.

The fix:
  1. `db_get_order_ticket_data` now returns `org_id` from the first row.
  2. `create_checks` enforces `ticket_org_id == user_org_id`, failing closed
     if either side is None.

P0 fix (2026-09): `create_checks` no longer resolves org_id via the
(now-deleted, ambiguous) `db_get_restaurant_by_id(user["branch_id"])` — it
reads the explicit `user["org_id"]` field directly (set by
deps.get_current_user from staff.org_id / the backfilled users.org_id
column). These tests now set org_id directly on the user dict instead of
mocking a restaurant lookup.
"""
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient

TICKET_ORG = 11
OTHER_ORG = 99
LOCATION_ID = 22

VALID_BODY = {
    "checks": [
        {
            "check_number": 1,
            "items": [{"name": "Pizza", "qty": 1, "unit_price": 1000}],
            "subtotal": 1000,
            "tax_amount": 0,
            "total": 1000,
        }
    ],
    "tax_pct": 0,
    "customer_name": "",
    "customer_email": "",
    "service_charge": 0.0,
    "tip_amount": 0.0,
}

TICKET_SAME_ORG = {
    "base_order_id": "order-abc",
    "table_name": "T1",
    "items": [{"name": "Pizza", "qty": 1, "quantity": 1, "price": 1000}],
    "total": 1000.0,
    "org_id": TICKET_ORG,
}

TICKET_NO_ORG = {
    "base_order_id": "order-abc",
    "table_name": "T1",
    "items": [{"name": "Pizza", "qty": 1, "quantity": 1, "price": 1000}],
    "total": 1000.0,
    # org_id intentionally absent
}

def _user_dict(org_id):
    return {
        "id": "u1",
        "username": "caja_user",
        "restaurant_name": "Test Resto",
        "branch_id": LOCATION_ID,
        "location_id": LOCATION_ID,
        "org_id": org_id,
        "restaurant_id": None,
        "role": "caja",
    }


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app)


def _patch_get_current_user(monkeypatch, user_dict):
    """Patch get_current_user in tables module to return a fixed dict."""
    async def _mock_get_user(request):
        return user_dict
    monkeypatch.setattr("app.routes.tables.get_current_user", _mock_get_user)


# ── Test 1: Same-org happy path ───────────────────────────────────────────────

def test_same_org_allowed(client, monkeypatch):
    """ticket.org_id == user_org_id → ownership check passes, db_create_checks called."""
    _patch_get_current_user(monkeypatch, _user_dict(TICKET_ORG))

    ticket_mock = AsyncMock(return_value=TICKET_SAME_ORG)
    create_mock = AsyncMock(return_value=[{"check_id": "chk1"}])

    with (
        patch("app.routes.tables.db.db_get_order_ticket_data", ticket_mock),
        patch("app.routes.tables.db.db_create_checks", create_mock),
    ):
        resp = client.post(
            "/api/table-orders/order-abc/checks",
            json=VALID_BODY,
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 200, resp.text
    create_mock.assert_awaited_once()


# ── Test 2: Cross-tenant blocked ─────────────────────────────────────────────

def test_cross_tenant_blocked(client, monkeypatch):
    """ticket.org_id=11, user org_id=99 → 403, db_create_checks NOT called."""
    _patch_get_current_user(monkeypatch, _user_dict(OTHER_ORG))

    ticket_mock = AsyncMock(return_value=TICKET_SAME_ORG)  # org_id=11
    create_mock = AsyncMock(return_value=[])

    with (
        patch("app.routes.tables.db.db_get_order_ticket_data", ticket_mock),
        patch("app.routes.tables.db.db_create_checks", create_mock),
    ):
        resp = client.post(
            "/api/table-orders/order-abc/checks",
            json=VALID_BODY,
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 403, resp.text
    create_mock.assert_not_awaited()


# ── Test 3: Missing org_id on ticket fails closed ────────────────────────────

def test_missing_ticket_org_id_fails_closed(client, monkeypatch):
    """ticket dict has no org_id key → fails closed with 403, no crash."""
    _patch_get_current_user(monkeypatch, _user_dict(TICKET_ORG))

    ticket_mock = AsyncMock(return_value=TICKET_NO_ORG)
    create_mock = AsyncMock(return_value=[])

    with (
        patch("app.routes.tables.db.db_get_order_ticket_data", ticket_mock),
        patch("app.routes.tables.db.db_create_checks", create_mock),
    ):
        resp = client.post(
            "/api/table-orders/order-abc/checks",
            json=VALID_BODY,
            headers={"Authorization": "Bearer test"},
        )

    assert resp.status_code == 403, resp.text
    create_mock.assert_not_awaited()
