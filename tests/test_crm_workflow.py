"""
tests/test_crm_workflow.py — Polish batch GAP D.

Covers the CRM internal pipeline workflow:
  - PATCH /prospects/{pid}/stage  — stage validation against VALID_STAGES
  - POST  /prospects/{pid}/convert — promote prospect to real organization

No live DB — uses superadmin session mock + repo function mocks.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def super_client(mock_superadmin_session):
    """Authenticated superadmin client (token 'superadmin' resolves OK)."""
    return TestClient(app)


HEADERS = {"Authorization": "Bearer superadmin"}


# ── Stage validation ──────────────────────────────────────────────────────────


class TestMoveStageValidation:

    def test_accepts_valid_stage(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", AsyncMock(return_value=None))

        resp = super_client.patch(
            "/api/internal/crm/prospects/42/stage",
            json={"stage": "demo"},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["stage"] == "demo"

    def test_rejects_unknown_stage(self, super_client):
        resp = super_client.patch(
            "/api/internal/crm/prospects/42/stage",
            json={"stage": "made_up_stage"},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert "stage inválido" in resp.json()["detail"]

    def test_rejects_empty_stage(self, super_client):
        resp = super_client.patch(
            "/api/internal/crm/prospects/42/stage",
            json={"stage": ""},
            headers=HEADERS,
        )
        assert resp.status_code == 400

    def test_normalizes_case(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_move_prospect_stage", AsyncMock(return_value=None))

        resp = super_client.patch(
            "/api/internal/crm/prospects/42/stage",
            json={"stage": "  CERRADO  "},
            headers=HEADERS,
        )
        assert resp.status_code == 200
        # Verify lowercase + trimmed value was forwarded to the repo
        crm_repo.db_move_prospect_stage.assert_awaited_once_with(42, "cerrado", lost_reason=None)


# ── Convert prospect to real organization ─────────────────────────────────────


def _sample_prospect(*, pid: int = 99, with_tag: bool = False, name: str = "Sushi Tokyo",
                     phone: str = "573009876543"):
    return {
        "id":              pid,
        "restaurant_name": name,
        "owner_name":      "María López",
        "phone":            phone,
        "city":             "Bogotá",
        "neighborhood":     "Chapinero",
        "category":         "japonesa",
        "stage":            "negociacion",
        "priority":         "high",
        "tags":             (["org:7"] if with_tag else []),
    }


class TestConvertProspect:

    def test_convert_success_returns_org_and_location(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        from app.repositories import restaurant_repo

        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=_sample_prospect()),
        )
        monkeypatch.setattr(
            crm_repo, "db_update_prospect",
            AsyncMock(return_value=_sample_prospect()),
        )
        monkeypatch.setattr(
            crm_repo, "db_create_prospect_note",
            AsyncMock(return_value={"id": 1}),
        )
        monkeypatch.setattr(
            restaurant_repo, "db_create_organization",
            AsyncMock(return_value={"id": 12, "name": "Sushi Tokyo"}),
        )
        monkeypatch.setattr(
            restaurant_repo, "db_create_location",
            AsyncMock(return_value={"id": 23, "name": "Principal", "org_id": 12}),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["success"] is True
        assert data["prospect_id"] == 99
        assert data["org"]["id"] == 12
        assert data["primary_location"]["id"] == 23

        # Side-effects
        crm_repo.db_update_prospect.assert_awaited_once()
        update_kwargs = crm_repo.db_update_prospect.await_args.args[1]
        assert update_kwargs["stage"] == "cerrado"
        assert "org:12" in update_kwargs["tags"]

    def test_convert_404_when_prospect_missing(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=None),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_convert_409_if_already_converted(self, super_client, monkeypatch):
        """Prospect already has org:N tag → return 409 instead of attempting create."""
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=_sample_prospect(with_tag=True)),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 409
        assert "ya fue convertido" in resp.json()["detail"].lower()

    def test_convert_400_if_missing_data(self, super_client, monkeypatch):
        """Prospect without restaurant_name AND no override → 400."""
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=_sample_prospect(name="", phone="")),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 400

    def test_convert_409_on_unique_violation(self, super_client, monkeypatch):
        """If db_create_organization raises UniqueViolation (whatsapp dupe) → 409."""
        import asyncpg
        from app.repositories.internal import crm_repo
        from app.repositories import restaurant_repo

        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=_sample_prospect()),
        )

        async def _raise(*a, **kw):
            raise asyncpg.UniqueViolationError("whatsapp dupe")

        monkeypatch.setattr(restaurant_repo, "db_create_organization", _raise)

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 409

    def test_convert_uses_body_overrides(self, super_client, monkeypatch):
        """body.name + body.whatsapp_number override prospect defaults."""
        from app.repositories.internal import crm_repo
        from app.repositories import restaurant_repo

        monkeypatch.setattr(
            crm_repo, "db_get_prospect_by_id",
            AsyncMock(return_value=_sample_prospect()),
        )
        monkeypatch.setattr(
            crm_repo, "db_update_prospect", AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            crm_repo, "db_create_prospect_note", AsyncMock(return_value={"id": 1}),
        )
        create_org_mock = AsyncMock(return_value={"id": 99, "name": "Override Name"})
        monkeypatch.setattr(restaurant_repo, "db_create_organization", create_org_mock)
        monkeypatch.setattr(
            restaurant_repo, "db_create_location",
            AsyncMock(return_value={"id": 100, "name": "Principal"}),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/99/convert",
            json={
                "name":            "Override Name",
                "whatsapp_number": "573001112222",
                "subscription_plan": "pro",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 200
        kwargs = create_org_mock.await_args.kwargs
        assert kwargs["name"] == "Override Name"
        assert kwargs["whatsapp_number"] == "573001112222"
        assert kwargs["subscription_plan"] == "pro"
