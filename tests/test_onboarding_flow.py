"""
tests/test_onboarding_flow.py

Unit tests for the onboarding automation flow:
  - POST /api/internal/crm/prospects/{pid}/convert  (extended with user creation + WA)
  - GET  /api/internal/admin/organizations/{org_id}/onboarding  (checklist endpoint)

All tests are unit-level (no real DB). DB calls are patched via monkeypatch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

HEADERS = {"Authorization": "Bearer superadmin"}


@pytest.fixture
def super_client(mock_superadmin_session):
    return TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sample_prospect(*, pid: int = 55, name: str = "Burger Palace", phone: str = "573001234567",
                     tags: list = None, email: str = "dueno@burgerpalace.com"):
    return {
        "id":              pid,
        "restaurant_name": name,
        "owner_name":      "Juan García",
        "phone":           phone,
        "email":           email,
        "city":            "Bogotá",
        "neighborhood":    "Chapinero",
        "category":        "hamburguesas",
        "stage":           "negociacion",
        "priority":        "high",
        "tags":            tags or [],
    }


def _mock_org(org_id: int = 10, name: str = "Burger Palace"):
    return {"id": org_id, "name": name, "created_at": "2026-01-01T10:00:00Z",
            "features": {}, "plan_code": "restaurante"}


def _mock_location(loc_id: int = 20, org_id: int = 10):
    return {"id": loc_id, "name": "Principal", "org_id": org_id}


def _patch_convert_deps(monkeypatch, *, org=None, loc=None, prospect=None,
                        user_created: bool = True, raise_org_exc=None):
    """Patch all DB/WA dependencies for the convert endpoint."""
    from app.repositories.internal import crm_repo
    from app.repositories import restaurant_repo

    org = org or _mock_org()
    loc = loc or _mock_location()
    p   = prospect or _sample_prospect()

    monkeypatch.setattr(crm_repo, "db_get_prospect_by_id", AsyncMock(return_value=p))
    monkeypatch.setattr(crm_repo, "db_update_prospect",    AsyncMock(return_value=p))
    monkeypatch.setattr(crm_repo, "db_create_prospect_note", AsyncMock(return_value={"id": 1}))

    if raise_org_exc:
        monkeypatch.setattr(restaurant_repo, "db_create_organization",
                            AsyncMock(side_effect=raise_org_exc))
    else:
        monkeypatch.setattr(restaurant_repo, "db_create_organization",
                            AsyncMock(return_value=org))

    monkeypatch.setattr(restaurant_repo, "db_create_location",
                        AsyncMock(return_value=loc))
    monkeypatch.setattr(restaurant_repo, "db_create_user",
                        AsyncMock(return_value=user_created))

    # Free trial (organizations.comp_until) — patched so the convert path is
    # exercised without a pool. Patched on the module the route imports from.
    from app.repositories import plan_limits_repo
    monkeypatch.setattr(plan_limits_repo, "db_set_comp_until", AsyncMock(return_value=None))

    # Audit log — best-effort, patch to avoid pool calls
    monkeypatch.setattr(
        "app.repositories.internal.audit_log_repo.db_log_audit_event",
        AsyncMock(return_value=1),
    )
    # The set-your-password code: a real reset row needs a DB; Resend "on".
    monkeypatch.setattr("app.repositories.password_reset_repo.db_create_password_reset",
                        AsyncMock(return_value="482913"))
    monkeypatch.setattr("app.services.email.delivers_for_real", lambda: True)


# ═══════════════════════════════════════════════════════════════════════════════
# Convert endpoint — happy path
# ═══════════════════════════════════════════════════════════════════════════════

class TestConvertWithOnboarding:
    """CRM convert opens the account WITHOUT a password (PM decision
    2026-10-02: Mesio never sets or sees one). The owner's email is the login
    and receives a code to create their own password."""

    def _convert(self, super_client, body=None, send_ok=True):
        with patch("app.services.email.send_email", new=AsyncMock(return_value=send_ok)) as mock_send:
            resp = super_client.post("/api/internal/crm/prospects/55/convert",
                                     json=body or {}, headers=HEADERS)
        return resp, mock_send

    def test_convert_creates_org_location_user_and_emails_the_code(self, super_client, monkeypatch):
        _patch_convert_deps(monkeypatch)
        resp, mock_send = self._convert(super_client, {"plan_code": "restaurante"})
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["ok"] is True and d["org_id"] == 10 and d["location_id"] == 20
        assert d["user"] == {"username": "dueno@burgerpalace.com"}
        assert d["welcome_message_sent"] is True
        sent = mock_send.await_args.kwargs
        assert sent["to"] == "dueno@burgerpalace.com"
        assert "482913" in sent["text"]
        assert "Contraseña temporal" not in sent["text"]

    def test_no_password_exists_anywhere(self, super_client, monkeypatch):
        """The stored hash is of a secret nobody saw; the response carries none."""
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch)
        resp, _ = self._convert(super_client)
        assert "password" not in resp.text
        pw_hash = restaurant_repo.db_create_user.await_args.kwargs["password_hash"]
        assert pw_hash.startswith("$2")

    def test_convert_needs_the_owner_email(self, super_client, monkeypatch):
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(email=""))
        resp, mock_send = self._convert(super_client)
        assert resp.status_code == 400
        restaurant_repo.db_create_organization.assert_not_awaited()
        mock_send.assert_not_awaited()

    def test_convert_owner_email_override_in_body(self, super_client, monkeypatch):
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(email=""))
        resp, mock_send = self._convert(super_client, {"owner_email": "Founder-Knows@BurgerPalace.com"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["username"] == "founder-knows@burgerpalace.com"
        assert mock_send.await_args.kwargs.get("to") == "founder-knows@burgerpalace.com"

    def test_convert_email_failure_still_returns_200(self, super_client, monkeypatch):
        _patch_convert_deps(monkeypatch)
        resp, _ = self._convert(super_client, send_ok=False)
        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["welcome_message_sent"] is False
        assert d["user"] is not None          # user was still created

    def test_console_email_is_not_reported_as_sent(self, super_client, monkeypatch):
        """Without Resend the "send" is a log line: the founder must know."""
        _patch_convert_deps(monkeypatch)
        monkeypatch.setattr("app.services.email.delivers_for_real", lambda: False)
        resp, _ = self._convert(super_client)
        assert resp.json()["welcome_message_sent"] is False

    def test_convert_default_plan_is_restaurante(self, super_client, monkeypatch):
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch)
        resp, _ = self._convert(super_client)
        assert resp.status_code == 200, resp.text
        assert restaurant_repo.db_create_organization.await_args.kwargs.get("plan_code") == "restaurante"

    def test_convert_starts_the_advertised_free_trial(self, super_client, monkeypatch):
        """N free days on top of the paid plan (status.md #12), N read from
        provisioning so the landing's promise and the grant cannot drift."""
        from datetime import datetime, timedelta, timezone
        from app.repositories import plan_limits_repo
        from app.services.provisioning import DEFAULT_TRIAL_DAYS
        _patch_convert_deps(monkeypatch)
        resp, _ = self._convert(super_client)
        assert resp.status_code == 200, resp.text
        args = plan_limits_repo.db_set_comp_until.await_args.args
        assert args[0] == 10
        expected = datetime.now(tz=timezone.utc) + timedelta(days=DEFAULT_TRIAL_DAYS)
        assert abs((args[1] - expected).total_seconds()) < 60
        assert resp.json()["comp_until"] is not None

    def test_convert_trial_days_zero_starts_no_trial(self, super_client, monkeypatch):
        from app.repositories import plan_limits_repo
        _patch_convert_deps(monkeypatch)
        resp, _ = self._convert(super_client, {"trial_days": 0})
        assert resp.status_code == 200, resp.text
        plan_limits_repo.db_set_comp_until.assert_not_awaited()
        assert resp.json()["comp_until"] is None

    def test_convert_survives_a_failed_trial_write(self, super_client, monkeypatch):
        from app.repositories import plan_limits_repo
        _patch_convert_deps(monkeypatch)
        monkeypatch.setattr(plan_limits_repo, "db_set_comp_until", AsyncMock(side_effect=RuntimeError("boom")))
        resp, _ = self._convert(super_client)
        assert resp.status_code == 200, resp.text
        assert resp.json()["comp_until"] is None

    def test_convert_404_missing_prospect(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id", AsyncMock(return_value=None))
        resp = super_client.post("/api/internal/crm/prospects/999/convert", json={}, headers=HEADERS)
        assert resp.status_code == 404

    def test_convert_409_already_converted(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id",
                            AsyncMock(return_value=_sample_prospect(tags=["org:5"])))
        resp = super_client.post("/api/internal/crm/prospects/55/convert", json={}, headers=HEADERS)
        assert resp.status_code == 409

    def test_convert_409_unique_violation(self, super_client, monkeypatch):
        import asyncpg
        _patch_convert_deps(monkeypatch, raise_org_exc=asyncpg.UniqueViolationError("dup"))
        resp, _ = self._convert(super_client)
        assert resp.status_code == 409

    def test_convert_400_missing_name(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id",
                            AsyncMock(return_value=_sample_prospect(name="", phone="")))
        resp = super_client.post("/api/internal/crm/prospects/55/convert", json={}, headers=HEADERS)
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# Onboarding checklist endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestOnboardingEndpoint:

    # The HQ tab reads the owner's own checklist (services/onboarding) since
    # 2026-10-02; its counts are tested in tests/test_onboarding*.py and
    # against a real DB in tests/test_hq_review_2026_10_02.py. Here: how the
    # endpoint turns that list into stages, score and "stalled".

    def _patch_onboarding(self, monkeypatch, *, created_at="2026-01-01T10:00:00", done=()):
        from app.repositories import restaurant_repo
        from app.services import onboarding

        monkeypatch.setattr(restaurant_repo, "db_get_org_by_id", AsyncMock(return_value={
            "id": 42, "name": "Test Org", "created_at": created_at, "features": {},
        }))
        steps = [
            {"key": k, "title": k, "detail": "", "done": k in done, "optional": k == "team", "actions": []}
            for k in ("menu", "tables", "team", "first_order")
        ]
        monkeypatch.setattr(onboarding, "get_checklist", AsyncMock(return_value={
            "steps": steps, "done": 0, "total": 3, "complete": False, "trial_days_left": 3,
        }))

    def _get(self, super_client):
        resp = super_client.get("/api/internal/admin/organizations/42/onboarding", headers=HEADERS)
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]

    def test_onboarding_all_done(self, super_client, monkeypatch):
        """Every required step done → 100, even with the optional team step pending."""
        self._patch_onboarding(monkeypatch, done=("menu", "tables", "first_order"))
        d = self._get(super_client)
        assert d["score"] == 100
        assert d["done_count"] == d["total_stages"] == 4
        assert d["stalled_count"] == 0
        assert {s["key"] for s in d["stages"]} == {"created", "menu", "tables", "team", "first_order"}

    def test_onboarding_only_created(self, super_client, monkeypatch):
        self._patch_onboarding(monkeypatch)
        d = self._get(super_client)
        assert d["done_count"] == 1
        assert d["score"] == 25
        assert d["trial_days_left"] == 3

    def test_onboarding_stalled_flags(self, super_client, monkeypatch):
        """An old account missing required steps is stalled; the optional one never is."""
        self._patch_onboarding(monkeypatch, created_at="2020-01-01T10:00:00")
        stages = {s["key"]: s for s in self._get(super_client)["stages"]}
        assert stages["menu"]["stalled"] and stages["tables"]["stalled"] and stages["first_order"]["stalled"]
        assert stages["team"]["stalled"] is False

    def test_onboarding_404_unknown_org(self, super_client, monkeypatch):
        from app.repositories import restaurant_repo
        monkeypatch.setattr(restaurant_repo, "db_get_org_by_id",
                            AsyncMock(return_value=None))

        resp = super_client.get(
            "/api/internal/admin/organizations/9999/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_onboarding_requires_auth(self, super_client):
        """No auth header → 401."""
        resp = super_client.get("/api/internal/admin/organizations/42/onboarding")
        assert resp.status_code == 401
