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
                     tags: list = None, email: str = ""):
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


# ═══════════════════════════════════════════════════════════════════════════════
# Convert endpoint — happy path
# ═══════════════════════════════════════════════════════════════════════════════

class TestConvertWithOnboarding:

    def test_convert_creates_org_location_user(self, super_client, monkeypatch):
        """Full convert: org + location + user created; welcome EMAIL patched
        to succeed. WhatsApp is retired for this call site — see
        app/services/email.py — the prospect needs an address (or the caller
        must pass body.owner_email) for the welcome message to go out."""
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(email="dueno@burgerpalace.com"))

        with patch("app.services.email.send_email",
                   new=AsyncMock(return_value=True)):
            resp = super_client.post(
                "/api/internal/crm/prospects/55/convert",
                json={"plan_code": "restaurante"},
                headers=HEADERS,
            )

        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["ok"] is True
        assert d["org_id"] == 10
        assert d["location_id"] == 20
        assert d["welcome_message_sent"] is True
        # User block present with username + temp_password
        assert d["user"] is not None
        assert "username" in d["user"]
        assert "temp_password" in d["user"]

    def test_convert_default_plan_is_restaurante(self, super_client, monkeypatch):
        """Default plan_code when body is empty should be 'restaurante' (CEO decision).

        Default `_sample_prospect()` has no email, so the welcome email is
        skipped gracefully — unrelated to what this test checks (plan_code)."""
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch)

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )

        assert resp.status_code == 200, resp.text
        # db_create_organization should have been called with plan="restaurante"
        restaurant_repo.db_create_organization.assert_awaited_once()
        call_kwargs = restaurant_repo.db_create_organization.await_args.kwargs
        assert call_kwargs.get("subscription_plan") == "restaurante"

    def test_convert_starts_the_advertised_free_trial(self, super_client, monkeypatch):
        """Closed product decision (docs/claude/status.md #12, revised
        2026-09-23): the sales hook is N free days on top of the PAID plan
        via organizations.comp_until. Nothing implemented it — db_set_comp_until
        had no caller at all, so every converted customer started billable on
        day one. N is read from provisioning rather than written here: the
        number the landing page promises and the number the product grants
        were 14 and 8 for a while, and a test that hardcodes one of them is
        how that goes unnoticed."""
        from datetime import datetime, timedelta, timezone
        from app.repositories import plan_limits_repo
        from app.services.provisioning import DEFAULT_TRIAL_DAYS
        _patch_convert_deps(monkeypatch)

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert", json={}, headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text

        plan_limits_repo.db_set_comp_until.assert_awaited_once()
        args = plan_limits_repo.db_set_comp_until.await_args.args
        assert args[0] == 10, "the trial must be set on the org just created"
        comp_until = args[1]
        expected = datetime.now(tz=timezone.utc) + timedelta(days=DEFAULT_TRIAL_DAYS)
        assert abs((comp_until - expected).total_seconds()) < 60, comp_until

        # And the founder is told, so they can quote the end date to the customer.
        assert resp.json()["comp_until"] is not None

    def test_convert_trial_days_zero_starts_no_trial(self, super_client, monkeypatch):
        """A customer who is already paying should not silently get free days."""
        from app.repositories import plan_limits_repo
        _patch_convert_deps(monkeypatch)

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={"trial_days": 0}, headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        plan_limits_repo.db_set_comp_until.assert_not_awaited()
        assert resp.json()["comp_until"] is None

    def test_convert_survives_a_failed_trial_write(self, super_client, monkeypatch):
        """The org already exists by then — never lose the conversion over the
        trial, but do not claim a trial that was not written either."""
        from app.repositories import plan_limits_repo
        _patch_convert_deps(monkeypatch)
        monkeypatch.setattr(
            plan_limits_repo, "db_set_comp_until",
            AsyncMock(side_effect=RuntimeError("boom")),
        )

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert", json={}, headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["org_id"] == 10
        assert resp.json()["comp_until"] is None

    def test_convert_skip_welcome_message(self, super_client, monkeypatch):
        """skip_welcome_message=True: creates org + user but does NOT call
        send_email — even though the prospect HAS an address (otherwise this
        test couldn't distinguish "skipped by flag" from "skipped, no email")."""
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(email="dueno@burgerpalace.com"))

        with patch("app.services.email.send_email",
                   new=AsyncMock(return_value=True)) as mock_send:
            resp = super_client.post(
                "/api/internal/crm/prospects/55/convert",
                json={"skip_welcome_message": True},
                headers=HEADERS,
            )

        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["welcome_message_sent"] is False
        mock_send.assert_not_awaited()

    def test_convert_email_failure_still_returns_200(self, super_client, monkeypatch):
        """If the welcome email send fails, convert still returns 200 with
        welcome_message_sent=False. WhatsApp is retired for this call site —
        see app/services/email.py."""
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(email="dueno@burgerpalace.com"))

        with patch("app.services.email.send_email",
                   new=AsyncMock(return_value=False)):
            resp = super_client.post(
                "/api/internal/crm/prospects/55/convert",
                json={},
                headers=HEADERS,
            )

        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["ok"] is True
        assert d["welcome_message_sent"] is False
        assert d["user"] is not None          # user was still created

    def test_convert_no_email_address_skips_gracefully(self, super_client, monkeypatch):
        """Prospect with no email and no body.owner_email override → welcome
        email is skipped (never attempted), convert still returns 200."""
        _patch_convert_deps(monkeypatch)  # default prospect: email=""

        with patch("app.services.email.send_email",
                   new=AsyncMock(return_value=True)) as mock_send:
            resp = super_client.post(
                "/api/internal/crm/prospects/55/convert",
                json={},
                headers=HEADERS,
            )

        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["welcome_message_sent"] is False
        mock_send.assert_not_awaited()

    def test_convert_owner_email_override_in_body(self, super_client, monkeypatch):
        """body.owner_email lets the founder supply an address the CRM row
        never captured — takes priority and the welcome email still sends."""
        _patch_convert_deps(monkeypatch)  # default prospect: email=""

        with patch("app.services.email.send_email",
                   new=AsyncMock(return_value=True)) as mock_send:
            resp = super_client.post(
                "/api/internal/crm/prospects/55/convert",
                json={"owner_email": "founder-knows@burgerpalace.com"},
                headers=HEADERS,
            )

        assert resp.status_code == 200, resp.text
        d = resp.json()
        assert d["welcome_message_sent"] is True
        mock_send.assert_awaited_once()
        assert mock_send.await_args.kwargs.get("to") == "founder-knows@burgerpalace.com"

    def test_convert_temp_password_in_response_not_none(self, super_client, monkeypatch):
        """temp_password is present in the response (founder needs to see it once)."""
        _patch_convert_deps(monkeypatch)

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )

        d = resp.json()
        pw = d["user"]["temp_password"]
        assert pw is not None
        assert len(pw) == 8
        # Must not contain confusable chars
        for bad in ("0", "O", "l", "1", "I"):
            assert bad not in pw, f"confusable char '{bad}' found in temp password"

    def test_convert_hashes_password_in_db(self, super_client, monkeypatch):
        """The password stored in DB (via db_create_user) must be a bcrypt hash, not plaintext."""
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch)

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )

        d = resp.json()
        assert resp.status_code == 200
        restaurant_repo.db_create_user.assert_awaited()
        call_args = restaurant_repo.db_create_user.await_args
        pw_hash = call_args.kwargs.get("password_hash") or (call_args.args[1] if len(call_args.args) > 1 else None)
        plaintext_pw = d["user"]["temp_password"]
        # The stored hash must not equal the plaintext
        assert pw_hash != plaintext_pw
        # bcrypt hashes start with $2b$ or $2a$
        assert pw_hash is not None
        assert pw_hash.startswith("$2")

    def test_convert_username_derived_from_prospect_name(self, super_client, monkeypatch):
        """Username is derived from prospect name when no email."""
        from app.repositories import restaurant_repo
        _patch_convert_deps(monkeypatch, prospect=_sample_prospect(name="Sushi Tokyo", phone="573001234567"))

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )

        assert resp.status_code == 200
        d = resp.json()
        username = d["user"]["username"]
        # Should be lowercased, dot-separated, derived from name
        assert username  # non-empty
        assert username == username.lower()

    def test_convert_404_missing_prospect(self, super_client, monkeypatch):
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id", AsyncMock(return_value=None))

        resp = super_client.post(
            "/api/internal/crm/prospects/999/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_convert_409_already_converted(self, super_client, monkeypatch):
        """Prospect with org:N tag → 409."""
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id",
                            AsyncMock(return_value=_sample_prospect(tags=["org:5"])))

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 409

    def test_convert_409_unique_violation(self, super_client, monkeypatch):
        """db_create_organization raises UniqueViolationError → 409."""
        import asyncpg
        _patch_convert_deps(monkeypatch, raise_org_exc=asyncpg.UniqueViolationError("dup"))

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 409

    def test_convert_400_missing_name_and_phone(self, super_client, monkeypatch):
        """Prospect without name and phone → 400."""
        from app.repositories.internal import crm_repo
        monkeypatch.setattr(crm_repo, "db_get_prospect_by_id",
                            AsyncMock(return_value=_sample_prospect(name="", phone="")))

        resp = super_client.post(
            "/api/internal/crm/prospects/55/convert",
            json={},
            headers=HEADERS,
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# Temp password generator unit test
# ═══════════════════════════════════════════════════════════════════════════════

class TestTempPasswordGenerator:

    def test_length(self):
        from app.routes.internal.crm import _generate_temp_password
        pw = _generate_temp_password()
        assert len(pw) == 8

    def test_no_confusable_chars(self):
        from app.routes.internal.crm import _generate_temp_password
        for _ in range(100):
            pw = _generate_temp_password()
            for bad in ("0", "O", "l", "1", "I"):
                assert bad not in pw, f"confusable '{bad}' in '{pw}'"

    def test_alphanumeric_only(self):
        import re
        from app.routes.internal.crm import _generate_temp_password
        for _ in range(50):
            pw = _generate_temp_password()
            assert re.fullmatch(r"[A-Za-z2-9]+", pw), f"non-alphanumeric chars in '{pw}'"


# ═══════════════════════════════════════════════════════════════════════════════
# Onboarding checklist endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestOnboardingEndpoint:

    def _patch_onboarding(self, monkeypatch, *, org=None, menu_val=None,
                           staff_first_at=None, features=None,
                           convo_first_at=None):
        """Patch restaurant_repo.db_get_org_by_id and the DB pool for onboarding queries."""
        from app.repositories import restaurant_repo

        org_data = org or {
            "id": 42,
            "name": "Test Org",
            "created_at": "2026-01-01T10:00:00",
            "features": features or {},
        }
        monkeypatch.setattr(restaurant_repo, "db_get_org_by_id",
                            AsyncMock(return_value=org_data))

        # Build pool mock for the 3 raw SQL queries in the endpoint
        # (menu query, staff query, conversation query)
        conn_mock = MagicMock()
        conn_mock.__aenter__ = AsyncMock(return_value=conn_mock)
        conn_mock.__aexit__  = AsyncMock(return_value=False)

        # Sequence of fetchrow results: menu_row, staff_row, convo_row
        menu_row  = {"menu": menu_val, "created_at": None}
        staff_row = {"first_at": staff_first_at}
        convo_row = {"first_at": convo_first_at}

        conn_mock.fetchrow = AsyncMock(side_effect=[menu_row, staff_row, convo_row])

        pool_mock = MagicMock()
        pool_mock.acquire = MagicMock(return_value=conn_mock)

        async def _fake_pool():
            return pool_mock

        # The endpoint does: from app.services.database import get_pool (lazy inside function)
        # Patching the canonical location is enough — the lazy import picks it up.
        monkeypatch.setattr("app.services.database.get_pool", _fake_pool)

    def test_onboarding_all_done(self, super_client, monkeypatch):
        """All stages done → score=100."""
        from datetime import datetime
        ts = datetime(2026, 1, 2, 12, 0, 0)
        self._patch_onboarding(
            monkeypatch,
            menu_val={"Hamburguesas": [{"name": "Clásica", "price": 15000}]},
            staff_first_at=ts,
            features={"wompi": {"public_key": "pub_test_abc"}},
            convo_first_at=ts,
        )

        resp = super_client.get(
            "/api/internal/admin/organizations/42/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["score"] == 100
        assert d["done_count"] == 5
        assert d["stalled_count"] == 0
        stages_by_key = {s["key"]: s for s in d["stages"]}
        assert stages_by_key["created"]["done"] is True
        assert stages_by_key["menu"]["done"] is True
        assert stages_by_key["staff"]["done"] is True
        assert stages_by_key["billing"]["done"] is True
        assert stages_by_key["first_convo"]["done"] is True

    def test_onboarding_only_created(self, super_client, monkeypatch):
        """No menu, no staff, no billing, no conversation → score=20 (1/5)."""
        self._patch_onboarding(
            monkeypatch,
            menu_val=None,
            staff_first_at=None,
            features={},
            convo_first_at=None,
        )

        resp = super_client.get(
            "/api/internal/admin/organizations/42/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 200, resp.text
        d = resp.json()["data"]
        assert d["done_count"] == 1      # only 'created'
        assert d["score"] == 20

    def test_onboarding_stalled_flags(self, super_client, monkeypatch):
        """Org created long ago with no menu → menu stage stalled."""
        self._patch_onboarding(
            monkeypatch,
            org={
                "id": 42,
                "name": "Old Org",
                "created_at": "2020-01-01T10:00:00",  # way past stall threshold
                "features": {},
            },
            menu_val=None,
            staff_first_at=None,
            features={},
            convo_first_at=None,
        )

        resp = super_client.get(
            "/api/internal/admin/organizations/42/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        d = resp.json()["data"]
        stages_by_key = {s["key"]: s for s in d["stages"]}
        assert stages_by_key["menu"]["stalled"] is True
        assert stages_by_key["staff"]["stalled"] is True
        assert stages_by_key["first_convo"]["stalled"] is True
        assert d["stalled_count"] >= 3

    def test_onboarding_404_unknown_org(self, super_client, monkeypatch):
        from app.repositories import restaurant_repo
        monkeypatch.setattr(restaurant_repo, "db_get_org_by_id",
                            AsyncMock(return_value=None))

        resp = super_client.get(
            "/api/internal/admin/organizations/9999/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 404

    def test_onboarding_billing_via_dian(self, super_client, monkeypatch):
        """dian_enabled=True counts as billing done even without wompi."""
        from datetime import datetime
        ts = datetime(2026, 1, 2, 12, 0, 0)
        self._patch_onboarding(
            monkeypatch,
            features={"dian_enabled": True},
            menu_val=None,
            staff_first_at=None,
            convo_first_at=None,
        )

        resp = super_client.get(
            "/api/internal/admin/organizations/42/onboarding",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        d = resp.json()["data"]
        stages_by_key = {s["key"]: s for s in d["stages"]}
        assert stages_by_key["billing"]["done"] is True

    def test_onboarding_requires_auth(self, super_client):
        """No auth header → 401."""
        resp = super_client.get("/api/internal/admin/organizations/42/onboarding")
        assert resp.status_code == 401
