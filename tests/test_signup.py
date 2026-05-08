"""
Smoke tests for the /api/signup public endpoint.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

VALID_PAYLOAD = {
    "nombre": "Juan Pérez",
    "email": "juan@restaurante.com",
    "telefono": "+57 310 000 0000",
    "restaurante": "El Rancho",
    "ciudad": "Medellín",
    "plan": "Pulso",
}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _mock_rate_ok():
    return patch("app.routes.signup_routes.state_store.rate_limit_check", new=AsyncMock(return_value=True))


def _mock_no_existing_prospect():
    return patch(
        "app.routes.signup_routes.crm_repo.db_get_prospect_by_phone",
        new=AsyncMock(return_value=None),
    )


def _mock_prospect(prospect_id: int = 42):
    return patch(
        "app.routes.signup_routes.crm_repo.db_create_prospect",
        new=AsyncMock(return_value={"id": prospect_id}),
    )


class TestSignupEndpoint:
    def test_get_signup_page_returns_200(self, client):
        r = client.get("/signup")
        assert r.status_code == 200
        assert "signup-form" in r.text

    def test_valid_payload_returns_ok(self, client):
        with _mock_rate_ok(), _mock_no_existing_prospect(), _mock_prospect(99):
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["prospect_id"] == 99

    def test_missing_required_field_returns_422(self, client):
        payload = {**VALID_PAYLOAD}
        del payload["email"]
        with _mock_rate_ok():
            r = client.post("/api/signup", json=payload)
        assert r.status_code == 422

    def test_invalid_email_returns_422(self, client):
        payload = {**VALID_PAYLOAD, "email": "not-an-email"}
        with _mock_rate_ok():
            r = client.post("/api/signup", json=payload)
        assert r.status_code == 422

    def test_invalid_plan_returns_422(self, client):
        payload = {**VALID_PAYLOAD, "plan": "FreeTier"}
        with _mock_rate_ok():
            r = client.post("/api/signup", json=payload)
        assert r.status_code == 422

    def test_rate_limited_returns_429(self, client):
        with patch("app.routes.signup_routes.state_store.rate_limit_check", new=AsyncMock(return_value=False)):
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 429

    def test_all_valid_plans_accepted(self, client):
        for plan in ("Pulso", "Restaurante", "Pro", "Cadena"):
            payload = {**VALID_PAYLOAD, "plan": plan}
            with _mock_rate_ok(), _mock_no_existing_prospect(), _mock_prospect():
                r = client.post("/api/signup", json=payload)
            assert r.status_code == 200, f"Plan {plan} returned {r.status_code}"
