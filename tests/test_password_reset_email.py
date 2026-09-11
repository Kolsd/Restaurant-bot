"""
tests/test_password_reset_email.py

Covers the email-channel migration of POST /api/auth/forgot-password
(app/routes/auth_routes.py). WhatsApp is being retired — this endpoint used
to deliver the OTP over WhatsApp; it now goes out via app/services/email.py.

No real DB, no real HTTP, no real email provider. `send_email` is patched at
its source (app.services.email.send_email) because auth_routes.py imports it
lazily inside the request handler (`from app.services.email import
send_email`) — patching the source module's attribute is what the lazy
import re-reads on every call.

Discipline (CLAUDE.md "Tests Verídicos" + the task brief):
  - Known vs unknown email must get a BYTE-IDENTICAL response (anti-enumeration).
  - A send failure (False or raised exception) must not crash the request —
    the caller (frontend) must still get its 200 response.
  - The OTP must never appear in anything logged by the request, end to end.
  - The existing per-email rate limit (3 / 15 min) must still trigger.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _unique_email(prefix: str) -> str:
    """A fresh email per test — the rate limiter's in-process fallback is a
    module-global dict keyed by email, so tests must not share keys."""
    return f"{prefix}.{uuid.uuid4().hex[:8]}@example.com"


def _patch_happy_path(monkeypatch, *, user: dict | None, code: str = "482913",
                       send_email_return=True, rate_limit_ok: bool = True):
    """Patch every dependency forgot_password touches, matching reality:
    db.db_get_user, password_reset_repo.db_create_password_reset,
    email.send_email, and the Redis-backed rate limiter."""
    import app.services.database as db_module
    import app.repositories.password_reset_repo as pr_repo
    import app.services.email as email_mod
    import app.services.state_store as state_store

    monkeypatch.setattr(db_module, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(pr_repo, "db_create_password_reset", AsyncMock(return_value=code))
    monkeypatch.setattr(state_store, "rate_limit_check", AsyncMock(return_value=rate_limit_ok))

    if callable(send_email_return) and not isinstance(send_email_return, AsyncMock):
        send_mock = AsyncMock(side_effect=send_email_return)
    else:
        send_mock = AsyncMock(return_value=send_email_return)
    monkeypatch.setattr(email_mod, "send_email", send_mock)
    return send_mock


class TestAntiEnumeration:
    def test_known_and_unknown_email_get_identical_response(self, monkeypatch):
        """A real account and a made-up address must return the exact same
        body and status — otherwise the endpoint becomes an account-existence
        oracle."""
        known_email = _unique_email("known")
        unknown_email = _unique_email("unknown")

        _patch_happy_path(monkeypatch, user={"username": known_email, "restaurant_name": "El Bogotazo"})
        resp_known = client.post("/api/auth/forgot-password", json={"email": known_email})

        _patch_happy_path(monkeypatch, user=None)
        resp_unknown = client.post("/api/auth/forgot-password", json={"email": unknown_email})

        assert resp_known.status_code == resp_unknown.status_code == 200
        assert resp_known.json() == resp_unknown.json() == {"sent": True, "channel": "email"}

    def test_lookup_error_still_generic_response(self, monkeypatch):
        """A DB error during user lookup must not change the response shape
        either — same anti-enumeration guarantee under failure."""
        import app.services.database as db_module
        import app.services.state_store as state_store

        monkeypatch.setattr(db_module, "db_get_user", AsyncMock(side_effect=RuntimeError("db down")))
        monkeypatch.setattr(state_store, "rate_limit_check", AsyncMock(return_value=True))

        resp = client.post("/api/auth/forgot-password", json={"email": _unique_email("dberror")})
        assert resp.status_code == 200
        assert resp.json() == {"sent": True, "channel": "email"}


class TestSendFailureDoesNotCrash:
    def test_send_returns_false_still_responds_200(self, monkeypatch):
        email = _unique_email("sendfails")
        send_mock = _patch_happy_path(
            monkeypatch, user={"username": email, "restaurant_name": "Test"}, send_email_return=False
        )
        resp = client.post("/api/auth/forgot-password", json={"email": email})

        assert resp.status_code == 200
        assert resp.json() == {"sent": True, "channel": "email"}
        send_mock.assert_awaited_once()

    def test_send_raises_still_responds_200(self, monkeypatch):
        """send_email is documented to never raise, but the route itself also
        wraps the call in try/except — verify that defense-in-depth actually
        holds if a provider client raised anyway."""
        email = _unique_email("sendraises")

        async def _boom(*args, **kwargs):
            raise RuntimeError("network exploded")

        _patch_happy_path(
            monkeypatch, user={"username": email, "restaurant_name": "Test"}, send_email_return=_boom
        )
        resp = client.post("/api/auth/forgot-password", json={"email": email})

        assert resp.status_code == 200
        assert resp.json() == {"sent": True, "channel": "email"}


class TestNoSecretLeakage:
    def test_otp_never_appears_in_any_log_call(self, monkeypatch):
        """End-to-end: the OTP code must never be passed to any log.* call —
        neither in auth_routes.py nor in email.py (which sends it in the
        email body)."""
        import app.routes.auth_routes as auth_routes_mod
        import app.services.email as email_mod

        email = _unique_email("secretcheck")
        secret_code = "739284"
        _patch_happy_path(
            monkeypatch, user={"username": email, "restaurant_name": "Test"}, code=secret_code
        )

        fake_auth_log = MagicMock()
        fake_email_log = MagicMock()
        monkeypatch.setattr(auth_routes_mod, "log", fake_auth_log)
        monkeypatch.setattr(email_mod, "log", fake_email_log)

        resp = client.post("/api/auth/forgot-password", json={"email": email})
        assert resp.status_code == 200

        for fake_log in (fake_auth_log, fake_email_log):
            for method_name in ("info", "warning", "error", "exception"):
                mock_method = getattr(fake_log, method_name)
                for call in mock_method.call_args_list:
                    call_args, call_kwargs = call
                    haystack = " ".join(str(a) for a in call_args) + " ".join(
                        str(v) for v in call_kwargs.values()
                    )
                    assert secret_code not in haystack, (
                        f"OTP leaked via {fake_log}.{method_name}({call_args}, {call_kwargs})"
                    )


class TestRateLimiting:
    def test_rate_limited_returns_not_sent(self, monkeypatch):
        """When the Redis-backed rate limiter says 'blocked', the endpoint
        must return {sent: False, channel: None} WITHOUT touching the DB or
        the email backend (still no enumeration signal — see docstring)."""
        import app.services.database as db_module
        import app.services.email as email_mod
        import app.services.state_store as state_store

        db_mock = AsyncMock(return_value={"username": "x", "restaurant_name": "x"})
        send_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(db_module, "db_get_user", db_mock)
        monkeypatch.setattr(email_mod, "send_email", send_mock)
        monkeypatch.setattr(state_store, "rate_limit_check", AsyncMock(return_value=False))

        resp = client.post("/api/auth/forgot-password", json={"email": _unique_email("throttled")})

        assert resp.status_code == 200
        assert resp.json() == {"sent": False, "channel": None}
        db_mock.assert_not_called()
        send_mock.assert_not_called()

    def test_fourth_request_in_window_is_blocked(self, monkeypatch):
        """Exercises the real state_store.rate_limit_check (in-process
        fallback, no Redis in this test env) rather than mocking it — proves
        the '3 per email per 15 min' limit from CLAUDE.md is actually wired,
        not just documented. Deliberately does NOT patch rate_limit_check."""
        import app.services.database as db_module
        import app.repositories.password_reset_repo as pr_repo
        import app.services.email as email_mod

        email = _unique_email("realratelimit")
        monkeypatch.setattr(db_module, "db_get_user", AsyncMock(return_value={"username": email}))
        monkeypatch.setattr(pr_repo, "db_create_password_reset", AsyncMock(return_value="111111"))
        monkeypatch.setattr(email_mod, "send_email", AsyncMock(return_value=True))

        responses = [
            client.post("/api/auth/forgot-password", json={"email": email}) for _ in range(4)
        ]
        bodies = [r.json() for r in responses]

        assert bodies[0] == bodies[1] == bodies[2] == {"sent": True, "channel": "email"}
        assert bodies[3] == {"sent": False, "channel": None}
