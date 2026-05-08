"""
tests/test_crm_lost_reason.py

Tests for CRM lost_reason capture (migration 0078).

Covers:
  - Move to perdido with reason: lost_reason persisted, lost_at set
  - Move to perdido without reason: 400
  - Move from perdido to active stage: lost_reason + lost_at cleared
"""
from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def super_client(mock_superadmin_session):
    return TestClient(app)


HEADERS = {"Authorization": "Bearer superadmin"}


# ── Move to perdido with reason ───────────────────────────────────────────────


class TestMoveToPerdidoWithReason:

    def test_persists_lost_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/10/stage",
            json={"stage": "perdido", "lost_reason": "Precio (muy caro)"},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stage"] == "perdido"

        # Verify repo was called with the reason
        mock_move.assert_awaited_once_with(10, "perdido", lost_reason="Precio (muy caro)")

    def test_persists_custom_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/11/stage",
            json={"stage": "perdido", "lost_reason": "Eligió un competidor"},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        mock_move.assert_awaited_once_with(11, "perdido", lost_reason="Eligió un competidor")


# ── Move to perdido without reason → 400 ─────────────────────────────────────


class TestMoveToPerdidoWithoutReason:

    def test_rejects_missing_lost_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/20/stage",
            json={"stage": "perdido"},
            headers=HEADERS,
        )
        assert resp.status_code == 400, resp.text
        assert "lost_reason" in resp.json()["detail"].lower()
        mock_move.assert_not_awaited()

    def test_rejects_null_lost_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/21/stage",
            json={"stage": "perdido", "lost_reason": None},
            headers=HEADERS,
        )
        assert resp.status_code == 400, resp.text
        mock_move.assert_not_awaited()

    def test_rejects_empty_lost_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/22/stage",
            json={"stage": "perdido", "lost_reason": ""},
            headers=HEADERS,
        )
        assert resp.status_code == 400, resp.text
        mock_move.assert_not_awaited()


# ── Move away from perdido → clears lost_reason ───────────────────────────────


class TestMoveAwayFromPerdido:

    def test_clears_lost_reason_when_moving_to_active_stage(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/30/stage",
            json={"stage": "respondio"},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stage"] == "respondio"
        # No lost_reason passed — repo called with None (clears on its side)
        mock_move.assert_awaited_once_with(30, "respondio", lost_reason=None)

    def test_valid_non_perdido_stage_passes_no_reason(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/31/stage",
            json={"stage": "demo"},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        mock_move.assert_awaited_once_with(31, "demo", lost_reason=None)


# ── lost_reason max_length validation ────────────────────────────────────────


class TestLostReasonValidation:

    def test_rejects_lost_reason_over_500_chars(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/40/stage",
            json={"stage": "perdido", "lost_reason": "x" * 501},
            headers=HEADERS,
        )
        assert resp.status_code == 422, resp.text
        mock_move.assert_not_awaited()

    def test_accepts_lost_reason_exactly_500_chars(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo

        mock_move = AsyncMock(return_value=None)
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", mock_move)

        resp = super_client.patch(
            "/api/internal/crm/prospects/41/stage",
            json={"stage": "perdido", "lost_reason": "x" * 500},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
