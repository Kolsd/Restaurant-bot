"""
tests/test_marketing_campaign.py — Polish batch GAP B.

Covers POST /api/marketing/send-campaign:
  - phone count validation (empty, too many)
  - message length validation (empty, too long)
  - rate limit (429)
  - disabled / cap_reached (403 / 402)
  - happy path: log_marketing_message called per phone
  - invalid phone in batch → status='invalid_phone' (skipped, others still send)
  - cap hit mid-batch → remaining phones marked blocked_cap
  - send failure (Meta API RuntimeError) → status='failed'

No live DB or HTTP; everything mocked.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import patch_auth


def _setup(monkeypatch, *, allowed=True, reason="ok", cap=30, sent_this_month=5,
           rate_ok=True, send_result=(True, "wam_test")):
    """Standard mocks for the campaign endpoint. Returns (client, mr)."""
    from app.main import app
    from app.repositories import marketing_repo as mr

    patch_auth(monkeypatch, restaurant_id=1)

    monkeypatch.setattr(mr, "can_send_marketing", AsyncMock(return_value=(allowed, reason)))
    monkeypatch.setattr(mr, "log_marketing_message", AsyncMock(return_value=1))

    monkeypatch.setattr(mr, "get_marketing_status", AsyncMock(return_value={
        "plan": "basic" if cap == 30 else ("pro" if cap == 300 else "enterprise"),
        "cap": cap,
        "sent_this_month": sent_this_month,
        "remaining": (cap - sent_this_month) if cap is not None else None,
        "enabled": True,
        "cost_estimate_month_usd": Decimal("0.5"),
    }))

    monkeypatch.setattr(
        "app.routes.marketing.state_store.rate_limit_check",
        AsyncMock(return_value=rate_ok),
    )
    monkeypatch.setattr(
        "app.routes.marketing._send_whatsapp_marketing",
        AsyncMock(return_value=send_result),
    )

    return TestClient(app), mr


class TestSendCampaignValidation:

    def test_empty_phone_list_returns_400(self, monkeypatch):
        client, _ = _setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": []},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400
        assert "empty_phone_list" in resp.text

    def test_too_many_phones_returns_400(self, monkeypatch):
        client, _ = _setup(monkeypatch)
        phones = [f"+5730000000{i:02d}" for i in range(51)]
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": phones},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400
        assert "too_many_phones" in resp.text

    def test_empty_message_returns_400(self, monkeypatch):
        client, _ = _setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "   ", "customer_phones": ["+573001234567"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400
        assert "empty_message" in resp.text

    def test_message_too_long_returns_400(self, monkeypatch):
        client, _ = _setup(monkeypatch)
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "x" * 1001, "customer_phones": ["+573001234567"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 400
        assert "message_too_long" in resp.text


class TestSendCampaignGuards:

    def test_rate_limited_returns_429(self, monkeypatch):
        client, _ = _setup(monkeypatch, rate_ok=False)
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": ["+573001234567"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 429

    def test_disabled_returns_403(self, monkeypatch):
        client, _ = _setup(monkeypatch, allowed=False, reason="disabled")
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": ["+573001234567"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 403

    def test_cap_reached_returns_402(self, monkeypatch):
        client, _ = _setup(monkeypatch, allowed=False, reason="cap_reached")
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": ["+573001234567"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 402


class TestSendCampaignBatch:

    def test_happy_path_logs_each_phone(self, monkeypatch):
        client, mr = _setup(monkeypatch)
        phones = [f"+5730000000{i:02d}" for i in range(3)]
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola amigos", "customer_phones": phones},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["sent"] == 3
        assert data["failed"] == 0
        assert data["skipped"] == 0
        # 1 log per phone (status='sent')
        assert mr.log_marketing_message.await_count == 3
        for call in mr.log_marketing_message.await_args_list:
            assert call.kwargs["status"] == "sent"
            assert call.kwargs["message_type"] == "campaign"

    def test_invalid_phone_in_batch_skipped(self, monkeypatch):
        client, mr = _setup(monkeypatch)
        phones = ["+573001234567", "not-a-phone", "+573009876543"]
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": phones},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] == 2
        assert data["skipped"] == 1
        # invalid_phone should appear in errors
        invalid_errs = [e for e in data["errors"] if e["error"] == "invalid_phone"]
        assert len(invalid_errs) == 1

    def test_cap_hit_mid_batch_marks_remaining_blocked(self, monkeypatch):
        """plan=basic, cap=30, sent_this_month=29 → only 1 of 3 phones sent."""
        client, mr = _setup(monkeypatch, cap=30, sent_this_month=29)
        phones = ["+573001111111", "+573002222222", "+573003333333"]
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": phones},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] == 1
        assert data["skipped"] == 2
        assert data["remaining"] == 0
        # blocked_cap should appear in errors
        blocked = [e for e in data["errors"] if e["error"] == "blocked_cap"]
        assert len(blocked) == 2

    def test_send_failure_returns_partial_success(self, monkeypatch):
        """If Meta API raises for one phone, others still send and `failed` increments."""
        client, mr = _setup(monkeypatch)
        # Toggle: 1st phone succeeds, 2nd raises, 3rd succeeds
        call_count = {"n": 0}

        async def _mixed_send(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("Meta API timeout")
            return (True, f"wam_{call_count['n']}")

        monkeypatch.setattr(
            "app.routes.marketing._send_whatsapp_marketing",
            _mixed_send,
        )

        phones = ["+573001111111", "+573002222222", "+573003333333"]
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": phones},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] == 2
        assert data["failed"] == 1
        # Verify the failed log entry has status='failed'
        failed_logs = [
            c for c in mr.log_marketing_message.await_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert len(failed_logs) == 1

    def test_enterprise_unlimited_no_remaining_cap(self, monkeypatch):
        """plan=enterprise → cap=None, remaining stays None throughout."""
        client, _ = _setup(monkeypatch, cap=None, sent_this_month=9999)
        resp = client.post(
            "/api/marketing/send-campaign",
            json={"message": "Hola", "customer_phones": ["+573001111111", "+573002222222"]},
            headers={"Authorization": "Bearer t"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] == 2
        assert data["remaining"] is None
