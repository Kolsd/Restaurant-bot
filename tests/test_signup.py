"""
Smoke tests for the /api/signup public endpoint (no DB).

Validation and error mapping only — provisioning is mocked here. What the
endpoint actually creates in Postgres is covered by
tests/test_self_serve_signup.py, which runs against a real database because
the failures it guards against are UNIQUE indexes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.provisioning import (
    ProvisionedTenant,
    ProvisioningError,
    TenantAlreadyExists,
)

VALID_PAYLOAD = {
    "nombre": "Juan Pérez",
    "email": "juan@restaurante.com",
    "telefono": "+57 310 000 0000",
    "restaurante": "El Rancho",
    "ciudad": "Medellín",
    "plan": "Esencial",
    "password": "claveSegura123",
}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _mock_rate_ok():
    return patch("app.routes.signup_routes.state_store.rate_limit_check", new=AsyncMock(return_value=True))


def _tenant(org_id: int = 99, username: str = "juan@restaurante.com") -> ProvisionedTenant:
    return ProvisionedTenant(
        org={"id": org_id, "name": "El Rancho", "slug": "el-rancho"},
        location={"id": org_id + 1, "name": "Principal"},
        username=username,
        user_created=True,
        comp_until=datetime.now(tz=timezone.utc) + timedelta(days=15),
        welcome_email_sent=False,
    )


def _mock_provisioning(result=None, exc=None):
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=result or _tenant())
    return patch("app.routes.signup_routes.create_tenant", new=mock)


def _mock_crm():
    """The CRM record is best-effort; silence it so it cannot mask a failure."""
    return patch("app.routes.signup_routes._record_prospect", new=AsyncMock(return_value=None))


class TestSignupEndpoint:
    def test_get_signup_page_returns_200(self, client):
        r = client.get("/signup")
        assert r.status_code == 200
        assert "signup-form" in r.text

    def test_valid_payload_creates_the_account(self, client):
        with _mock_rate_ok(), _mock_provisioning(), _mock_crm():
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["org_id"] == 99
        assert body["location_id"] == 100
        # The page needs these to tell the owner how to get in without
        # waiting for an email that may never be configured.
        assert body["username"] == "juan@restaurante.com"
        assert body["login_url"] == "/login"
        assert body["trial_days"] == 15
        assert body["trial_until"]

    def test_the_owner_password_is_passed_through_untouched(self, client):
        """Whitespace is part of a password; trimming it locks the owner out."""
        payload = {**VALID_PAYLOAD, "password": "  espacios  al  borde  "}
        with _mock_rate_ok(), _mock_provisioning() as mock, _mock_crm():
            r = client.post("/api/signup", json=payload)
        assert r.status_code == 200
        assert mock.await_args.kwargs["password"] == "  espacios  al  borde  "
        # And the phone never becomes the org's WhatsApp line (UNIQUE index).
        assert "whatsapp_number" not in mock.await_args.kwargs

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

    def test_short_password_returns_422(self, client):
        payload = {**VALID_PAYLOAD, "password": "corta"}
        with _mock_rate_ok():
            r = client.post("/api/signup", json=payload)
        assert r.status_code == 422

    def test_rate_limited_returns_429(self, client):
        with patch("app.routes.signup_routes.state_store.rate_limit_check", new=AsyncMock(return_value=False)):
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 429

    def test_existing_account_returns_409_not_500(self, client):
        """The page turns this into "entra desde /login", so it must not be a 500."""
        exc = TenantAlreadyExists("user", "Ya existe una cuenta con ese correo.")
        with _mock_rate_ok(), _mock_provisioning(exc=exc), _mock_crm():
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 409
        assert "cuenta" in r.json()["detail"].lower()

    def test_provisioning_failure_returns_500_with_its_message(self, client):
        exc = ProvisioningError("location", "Falló la creación de la sede.")
        with _mock_rate_ok(), _mock_provisioning(exc=exc), _mock_crm():
            r = client.post("/api/signup", json=VALID_PAYLOAD)
        assert r.status_code == 500
        assert r.json()["detail"] == "Falló la creación de la sede."

    def test_all_valid_plans_accepted(self, client):
        for plan in ("Esencial", "Restaurante", "Pro", "Cadena"):
            payload = {**VALID_PAYLOAD, "plan": plan}
            with _mock_rate_ok(), _mock_provisioning() as mock, _mock_crm():
                r = client.post("/api/signup", json=payload)
            assert r.status_code == 200, f"Plan {plan} returned {r.status_code}"
            assert mock.await_args.kwargs["plan_code"] == plan.lower()
