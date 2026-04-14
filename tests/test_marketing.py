"""
Suite — Marketing System (Clientes en Riesgo)
tests/test_marketing.py

Covers:
  A — MARKETING_CAPS constants and can_send_marketing logic (4 tests)
  B — count_marketing_this_month tz-aware boundary (2 tests)
  C — log_marketing_message fields and cost default (2 tests)
  D — get_at_risk_customers filtering and ordering (2 tests)
  E — POST /api/marketing/send-reengagement endpoint (6 tests)
  F — GET endpoints shape + auth required (3 tests)
  G — Decimal precision in cost accumulation (1 test)

Total: 20 tests.  No live DB, no real HTTP calls.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_pool, make_row, patch_auth


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Mock helpers ───────────────────────────────────────────────────────────────

def _make_conn(fetchrow=None, fetch=None, fetchval=None, execute=None):
    conn = MagicMock()
    conn.fetchrow  = AsyncMock(return_value=fetchrow)
    conn.fetch     = AsyncMock(return_value=fetch or [])
    conn.fetchval  = AsyncMock(return_value=fetchval)
    conn.execute   = AsyncMock(return_value=execute or "UPDATE 1")
    return conn


def _make_restaurant_row(plan: str = "basic", enabled: bool = True):
    return make_row({
        "plan": plan,
        "marketing_enabled": enabled,
        "tz": "America/Bogota",
        "country_code": "CO",
        "features": json.dumps({"timezone": "America/Bogota", "country_code": "CO"}),
    })


# ══════════════════════════════════════════════════════════════════════════════
# Block A — MARKETING_CAPS and can_send_marketing logic
# ══════════════════════════════════════════════════════════════════════════════

class TestMarketingCaps:

    def test_A1_basic_cap_is_30(self):
        from app.repositories.marketing_repo import MARKETING_CAPS
        assert MARKETING_CAPS["basic"] == 30

    def test_A2_pro_cap_is_300(self):
        from app.repositories.marketing_repo import MARKETING_CAPS
        assert MARKETING_CAPS["pro"] == 300

    def test_A3_enterprise_cap_is_none(self):
        """enterprise plan → None means unlimited."""
        from app.repositories.marketing_repo import MARKETING_CAPS
        assert MARKETING_CAPS["enterprise"] is None

    def test_A4_can_send_disabled(self, monkeypatch):
        """marketing_enabled=False → (False, 'disabled') regardless of cap."""
        from app.repositories import marketing_repo

        row = _make_restaurant_row(plan="pro", enabled=False)
        conn = _make_conn(fetchrow=row)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        allowed, reason = _run(marketing_repo.can_send_marketing(1))
        assert allowed is False
        assert reason == "disabled"

    def test_A5_can_send_cap_reached(self, monkeypatch):
        """basic plan at 30/30 → cap_reached."""
        from app.repositories import marketing_repo

        row = _make_restaurant_row(plan="basic", enabled=True)
        conn = _make_conn(fetchrow=row)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))
        # Mock count_marketing_this_month to return 30 (at cap)
        monkeypatch.setattr(
            marketing_repo, "count_marketing_this_month",
            AsyncMock(return_value=30),
        )

        allowed, reason = _run(marketing_repo.can_send_marketing(1))
        assert allowed is False
        assert reason == "cap_reached"

    def test_A6_enterprise_always_allowed(self, monkeypatch):
        """enterprise plan with 1000 sent → still allowed (unlimited)."""
        from app.repositories import marketing_repo

        row = _make_restaurant_row(plan="enterprise", enabled=True)
        conn = _make_conn(fetchrow=row)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))
        # Should NOT call count_marketing_this_month for enterprise
        monkeypatch.setattr(
            marketing_repo, "count_marketing_this_month",
            AsyncMock(return_value=1000),
        )

        allowed, reason = _run(marketing_repo.can_send_marketing(1))
        assert allowed is True
        assert reason == "ok"

    def test_A7_restaurant_not_found(self, monkeypatch):
        """No row returned → (False, 'restaurant_not_found')."""
        from app.repositories import marketing_repo

        conn = _make_conn(fetchrow=None)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        allowed, reason = _run(marketing_repo.can_send_marketing(999))
        assert allowed is False
        assert reason == "restaurant_not_found"


# ══════════════════════════════════════════════════════════════════════════════
# Block B — count_marketing_this_month tz-aware boundary
# ══════════════════════════════════════════════════════════════════════════════

class TestCountMarketingThisMonth:

    def test_B1_count_returns_integer(self, monkeypatch):
        """DB returns a count, function returns int."""
        from app.repositories import marketing_repo

        conn = _make_conn(fetchval=5)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        result = _run(marketing_repo.count_marketing_this_month(1, "America/Bogota"))
        assert result == 5
        assert isinstance(result, int)

    def test_B2_previous_month_messages_do_not_count(self, monkeypatch):
        """Messages from previous month don't count — verified via the SQL window passed to fetchval."""
        from app.repositories import marketing_repo

        # Mock that returns 0 — simulating previous month messages filtered out
        conn = _make_conn(fetchval=0)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        result = _run(marketing_repo.count_marketing_this_month(1, "America/Bogota"))
        # Verify the query used tz-aware month boundaries
        call_args = conn.fetchval.call_args
        assert call_args is not None
        # The query should have month_start and month_end as args $2 and $3
        args = call_args[0]
        # args[0] is the SQL, args[1] is restaurant_id, args[2] is month_start, args[3] is month_end
        month_start = args[2]
        month_end   = args[3]
        # month_start must be before month_end
        assert month_start < month_end
        # month_end should be in the next month
        assert month_end.month != month_start.month or month_end.year > month_start.year
        assert result == 0


# ══════════════════════════════════════════════════════════════════════════════
# Block C — log_marketing_message
# ══════════════════════════════════════════════════════════════════════════════

class TestLogMarketingMessage:

    def test_C1_inserts_with_correct_fields(self, monkeypatch):
        """log_marketing_message passes all fields to the DB insert."""
        from app.repositories import marketing_repo

        conn = _make_conn(fetchval=42)
        # _get_restaurant_country also uses conn.fetchrow
        conn.fetchrow = AsyncMock(return_value=make_row({
            "features": json.dumps({"country_code": "CO"}),
        }))
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        row_id = _run(marketing_repo.log_marketing_message(
            restaurant_id=1,
            customer_phone="+573001234567",
            message_type="reengagement",
            status="sent",
            message_body="Hola 👋",
            cost_estimate_usd=Decimal("0.0625"),
            meta_message_id="wam_abc123",
        ))

        assert row_id == 42
        # Verify fetchval was called (the INSERT RETURNING id)
        conn.fetchval.assert_called_once()
        sql, *params = conn.fetchval.call_args[0]
        assert "INSERT INTO marketing_messages_log" in sql
        assert "+573001234567" in params
        assert "reengagement" in params
        assert "sent" in params

    def test_C2_default_cost_used_when_none(self, monkeypatch):
        """When cost_estimate_usd is None, it's resolved from country default."""
        from app.repositories import marketing_repo

        # Simulate CO restaurant → cost = 0.0625
        conn = _make_conn(fetchval=7)
        conn.fetchrow = AsyncMock(return_value=make_row({
            "features": json.dumps({"country_code": "CO"}),
        }))
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        row_id = _run(marketing_repo.log_marketing_message(
            restaurant_id=1,
            customer_phone="+573001234567",
            message_type="reengagement",
            status="blocked_cap",
            cost_estimate_usd=None,  # should default
        ))

        assert row_id == 7
        # Inspect that cost passed to fetchval is Decimal("0.0625")
        _, *params = conn.fetchval.call_args[0]
        # cost_estimate_usd is the 6th positional param ($6)
        # params order: restaurant_id, phone, type, template, body, cost, status, error, meta_id
        cost_param = params[5]
        assert isinstance(cost_param, Decimal)
        assert cost_param == Decimal("0.06")  # quantize_money 2 decimals → 0.0625 rounds to 0.06 for USD


# ══════════════════════════════════════════════════════════════════════════════
# Block D — get_at_risk_customers
# ══════════════════════════════════════════════════════════════════════════════

class TestGetAtRiskCustomers:

    def _make_customer_row(self, phone, days_since, total_orders, total_spent, last_seen=None):
        if last_seen is None:
            last_seen = datetime.now(timezone.utc) - timedelta(days=days_since)
        return make_row({
            "customer_phone": phone,
            "customer_name":  "Test Customer",
            "last_order_at":  last_seen,
            "days_since":     days_since,
            "total_orders":   total_orders,
            "total_spent":    total_spent,
        })

    def test_D1_returns_only_qualifying_customers(self, monkeypatch):
        """Only customers with >= 3 orders and >= 21 days dormant appear."""
        from app.repositories import marketing_repo

        rows = [
            self._make_customer_row("+57300111", 25, 5, Decimal("150000")),
            self._make_customer_row("+57300222", 30, 3, Decimal("90000")),
        ]
        conn = _make_conn(fetch=rows)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        result = _run(marketing_repo.get_at_risk_customers(1, limit=50))
        assert len(result) == 2
        assert result[0]["customer_phone"] == "+57300111"
        assert result[0]["days_since"] == 25
        assert result[0]["total_orders"] == 5

        # Verify SQL filters are in the query
        call_args = conn.fetch.call_args[0]
        sql = call_args[0]
        assert "total_orders" in sql
        assert "MAKE_INTERVAL" in sql

    def test_D2_ordered_by_days_since_desc(self, monkeypatch):
        """Result list is ordered most dormant first (days_since DESC)."""
        from app.repositories import marketing_repo

        # DB returns sorted already (ORDER BY last_seen ASC = days_since DESC)
        rows = [
            self._make_customer_row("+57300333", 45, 7, Decimal("200000")),
            self._make_customer_row("+57300444", 22, 4, Decimal("80000")),
        ]
        conn = _make_conn(fetch=rows)
        pool = make_pool(conn)

        monkeypatch.setattr(marketing_repo, "_get_pool", AsyncMock(return_value=pool))

        result = _run(marketing_repo.get_at_risk_customers(1))
        assert result[0]["days_since"] >= result[1]["days_since"]


# ══════════════════════════════════════════════════════════════════════════════
# Block E — POST /api/marketing/send-reengagement endpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestSendReengagementEndpoint:

    def _setup(self, monkeypatch, *, plan="basic", enabled=True, can_send=True, can_send_reason="ok"):
        from app.main import app
        from app.repositories import marketing_repo as mr
        from app.routes import marketing as mkt_routes

        patch_auth(monkeypatch, restaurant_id=1)

        # Patch can_send_marketing
        monkeypatch.setattr(mr, "can_send_marketing", AsyncMock(return_value=(can_send, can_send_reason)))

        # Patch log_marketing_message
        monkeypatch.setattr(mr, "log_marketing_message", AsyncMock(return_value=1))

        # Patch get_marketing_status
        cap = {"basic": 30, "pro": 300}[plan] if plan != "enterprise" else None
        monkeypatch.setattr(mr, "get_marketing_status", AsyncMock(return_value={
            "plan": plan,
            "cap": cap,
            "sent_this_month": 5,
            "remaining": (cap - 5) if cap is not None else None,
            "enabled": enabled,
            "cost_estimate_month_usd": Decimal("0.3125"),
        }))

        # Patch state_store.rate_limit_check → allowed by default
        monkeypatch.setattr(
            "app.routes.marketing.state_store.rate_limit_check",
            AsyncMock(return_value=True),
        )

        # Patch _send_whatsapp_marketing → success
        monkeypatch.setattr(
            "app.routes.marketing._send_whatsapp_marketing",
            AsyncMock(return_value=(True, "wam_test_id")),
        )

        return TestClient(app)

    def test_E1_happy_path_returns_200(self, monkeypatch):
        client = self._setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "remaining" in data

    def test_E2_invalid_phone_returns_400(self, monkeypatch):
        client = self._setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "not-a-phone"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 400
        assert "invalid_phone" in resp.text

    def test_E3_rate_limited_returns_429(self, monkeypatch):
        client = self._setup(monkeypatch)
        # Override rate limit to block
        monkeypatch.setattr(
            "app.routes.marketing.state_store.rate_limit_check",
            AsyncMock(return_value=False),
        )
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 429
        assert "rate_limited" in resp.text

    def test_E4_cap_reached_returns_402(self, monkeypatch):
        client = self._setup(monkeypatch, can_send=False, can_send_reason="cap_reached")
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 402
        assert "cap_reached" in resp.text

    def test_E5_disabled_returns_403(self, monkeypatch):
        client = self._setup(monkeypatch, can_send=False, can_send_reason="disabled")
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 403
        assert "marketing_disabled" in resp.text

    def test_E6_send_failure_returns_502(self, monkeypatch):
        client = self._setup(monkeypatch)
        monkeypatch.setattr(
            "app.routes.marketing._send_whatsapp_marketing",
            AsyncMock(side_effect=RuntimeError("Meta API error")),
        )
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 502
        assert "send_failed" in resp.text

    def test_E7_logs_blocked_cap(self, monkeypatch):
        """When cap reached, log_marketing_message is called with status='blocked_cap'."""
        from app.repositories import marketing_repo as mr

        client = self._setup(monkeypatch, can_send=False, can_send_reason="cap_reached")
        client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )

        mr.log_marketing_message.assert_called_once()
        call_kwargs = mr.log_marketing_message.call_args[1]
        assert call_kwargs["status"] == "blocked_cap"

    def test_E8_logs_blocked_disabled(self, monkeypatch):
        """When disabled, log_marketing_message is called with status='blocked_disabled'."""
        from app.repositories import marketing_repo as mr

        client = self._setup(monkeypatch, can_send=False, can_send_reason="disabled")
        client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567"},
            headers={"Authorization": "Bearer test"},
        )

        mr.log_marketing_message.assert_called_once()
        call_kwargs = mr.log_marketing_message.call_args[1]
        assert call_kwargs["status"] == "blocked_disabled"

    def test_E9_message_too_long_returns_400(self, monkeypatch):
        client = self._setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-reengagement",
            json={"customer_phone": "+573001234567", "message": "x" * 1025},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 400
        assert "message_too_long" in resp.text


# ══════════════════════════════════════════════════════════════════════════════
# Block F — GET endpoints shape and auth
# ══════════════════════════════════════════════════════════════════════════════

class TestGetEndpoints:

    def test_F1_marketing_status_shape(self, monkeypatch):
        from app.main import app
        from app.repositories import marketing_repo as mr

        patch_auth(monkeypatch, restaurant_id=1)
        monkeypatch.setattr(mr, "get_marketing_status", AsyncMock(return_value={
            "plan": "basic",
            "cap": 30,
            "sent_this_month": 5,
            "remaining": 25,
            "enabled": True,
            "cost_estimate_month_usd": Decimal("0.3125"),
        }))

        client = TestClient(app)
        resp = client.get("/api/marketing/status", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] == "basic"
        assert data["cap"] == 30
        assert data["remaining"] == 25
        assert isinstance(data["cost_estimate_month_usd"], float)

    def test_F2_at_risk_shape(self, monkeypatch):
        from app.main import app
        from app.repositories import marketing_repo as mr

        patch_auth(monkeypatch, restaurant_id=1)
        monkeypatch.setattr(mr, "get_at_risk_customers", AsyncMock(return_value=[
            {
                "customer_phone": "+57300111",
                "customer_name": "Ana Test",
                "last_order_at": "2026-03-15T10:00:00+00:00",
                "days_since": 29,
                "total_orders": 5,
                "total_spent": 150000.0,
            }
        ]))

        client = TestClient(app)
        resp = client.get("/api/customers/at-risk", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        data = resp.json()
        assert "customers" in data
        assert "count" in data
        assert data["count"] == 1
        assert data["customers"][0]["customer_phone"] == "+57300111"

    def test_F3_marketing_history_shape(self, monkeypatch):
        from app.main import app
        from app.repositories import marketing_repo as mr

        patch_auth(monkeypatch, restaurant_id=1)
        monkeypatch.setattr(mr, "get_recent_marketing_messages", AsyncMock(return_value=[
            {
                "id": 1,
                "customer_phone": "+57300111",
                "message_type": "reengagement",
                "template_name": None,
                "message_body": "Hola",
                "sent_at": "2026-04-10T12:00:00+00:00",
                "cost_estimate_usd": 0.0625,
                "status": "sent",
                "error": None,
                "meta_message_id": "wam_abc",
            }
        ]))

        client = TestClient(app)
        resp = client.get("/api/marketing/history", headers={"Authorization": "Bearer test"})
        assert resp.status_code == 200
        data = resp.json()
        assert "messages" in data
        assert data["messages"][0]["status"] == "sent"

    def test_F4_auth_required_on_status(self, monkeypatch):
        """Without a valid token, /api/marketing/status should return 401."""
        from app.main import app
        from unittest.mock import AsyncMock

        # verify_token returns None for invalid/missing token
        monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value=None))

        client = TestClient(app)
        resp = client.get("/api/marketing/status")
        assert resp.status_code == 401

    def test_F5_patch_settings_updates_enabled(self, monkeypatch):
        from app.main import app
        from app.repositories import marketing_repo as mr

        patch_auth(monkeypatch, restaurant_id=1)
        monkeypatch.setattr(mr, "toggle_marketing_enabled", AsyncMock(return_value=True))

        client = TestClient(app)
        resp = client.patch(
            "/api/marketing/settings",
            json={"enabled": False},
            headers={"Authorization": "Bearer test"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["marketing_enabled"] is False
        mr.toggle_marketing_enabled.assert_called_once_with(1, False)


# ══════════════════════════════════════════════════════════════════════════════
# Block G — Decimal precision
# ══════════════════════════════════════════════════════════════════════════════

class TestDecimalPrecision:

    def test_G1_cost_accumulation_no_float_drift(self):
        """Summing 300 messages at $0.0625 must not produce float drift."""
        from app.repositories.marketing_repo import MARKETING_COST_USD_DEFAULT
        from app.services.money import quantize_money

        cost_per = MARKETING_COST_USD_DEFAULT  # Decimal("0.0625")
        # 300 pro messages
        total = sum(cost_per for _ in range(300))
        total_quantized = quantize_money(total, currency="USD")

        # Expected: 300 × 0.0625 = 18.75 exactly
        assert total_quantized == Decimal("18.75")
        # Prove it's not float (float would give something like 18.750000000000004)
        assert isinstance(total_quantized, Decimal)

    def test_G2_mx_cost_is_different_from_default(self):
        """Mexico has a different cost rate than CO."""
        from app.repositories.marketing_repo import (
            MARKETING_COSTS_USD_BY_COUNTRY,
            MARKETING_COST_USD_DEFAULT,
        )
        mx_cost = MARKETING_COSTS_USD_BY_COUNTRY["MX"]
        assert mx_cost < MARKETING_COST_USD_DEFAULT
        assert mx_cost == Decimal("0.0436")
