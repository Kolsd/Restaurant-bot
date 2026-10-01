"""
tests/test_email.py

Unit tests for app/services/email.py (provider-agnostic transactional email)
and app/services/email_templates.py.

Context: Mesio is retiring WhatsApp. Password resets
and the CRM welcome message all now go out via this module. There is NO
Resend/SendGrid account yet — EMAIL_BACKEND defaults to "console" and MUST
work with zero credentials so a missing/unset provider key can never
silently break a deploy. Real provider HTTP (Resend) is mocked via respx;
this suite never touches the real network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from httpx import Response

from app.services import email as email_mod
from app.services.email_templates import (
    render_password_reset_email,
    render_welcome_email,
)


@pytest.fixture(autouse=True)
def _clean_email_env(monkeypatch):
    """Every test controls EMAIL_BACKEND / RESEND_API_KEY / EMAIL_FROM
    explicitly rather than inheriting whatever is in the shell environment."""
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)


# ══════════════════════════════════════════════════════════════════════════
# Console backend (the default — no credentials required)
# ══════════════════════════════════════════════════════════════════════════

class TestConsoleBackend:
    async def test_default_backend_is_console_and_returns_true(self):
        """No EMAIL_BACKEND set at all → defaults to console → True.
        This is the invariant that keeps a missing provider key from ever
        silently breaking a deploy."""
        ok = await email_mod.send_email(
            to="owner@example.com", subject="Hola", html="<p>hola</p>", text="hola"
        )
        assert ok is True

    async def test_explicit_console_backend_returns_true(self, monkeypatch):
        monkeypatch.setenv("EMAIL_BACKEND", "console")
        ok = await email_mod.send_email(to="owner@example.com", subject="Hola", html="<p>hola</p>")
        assert ok is True

    async def test_unknown_backend_value_falls_back_to_console(self, monkeypatch):
        """A typo'd/aspirational EMAIL_BACKEND must not crash — falls back to
        console with a warning."""
        monkeypatch.setenv("EMAIL_BACKEND", "mailgun")
        fake_log = MagicMock()
        monkeypatch.setattr(email_mod, "log", fake_log)

        ok = await email_mod.send_email(to="owner@example.com", subject="x", html="x")

        assert ok is True
        assert fake_log.warning.called

    async def test_invalid_recipient_returns_false(self):
        assert await email_mod.send_email(to="", subject="x", html="x") is False
        assert await email_mod.send_email(to="not-an-email", subject="x", html="x") is False
        assert await email_mod.send_email(to=None, subject="x", html="x") is False

    async def test_console_backend_never_logs_the_body(self, monkeypatch):
        """The body carries reset codes / temp passwords. The console
        backend must log only recipient/subject/lengths — never `html` or
        `text` content (see module docstring + mask_email convention)."""
        fake_log = MagicMock()
        monkeypatch.setattr(email_mod, "log", fake_log)
        secret = "839217-SECRET-CODE"
        html = f"<p>Tu código: {secret}</p>"
        text = f"Tu código: {secret}"

        await email_mod.send_email(to="owner@example.com", subject="Reset", html=html, text=text)

        assert fake_log.info.called
        for call in fake_log.info.call_args_list:
            args, kwargs = call
            haystack = " ".join(str(a) for a in args) + " ".join(str(v) for v in kwargs.values())
            assert secret not in haystack

    async def test_console_backend_masks_recipient(self, monkeypatch):
        """mask_email() convention: only first char + domain should appear,
        never the full address, in the logged `to` field."""
        fake_log = MagicMock()
        monkeypatch.setattr(email_mod, "log", fake_log)

        await email_mod.send_email(to="verysecretlocal@example.com", subject="x", html="x")

        logged_to = fake_log.info.call_args.kwargs.get("to")
        assert logged_to == "v***@example.com"
        assert "verysecretlocal" not in logged_to


# ══════════════════════════════════════════════════════════════════════════
# Resend backend (the one real provider — no account exists yet)
# ══════════════════════════════════════════════════════════════════════════

class TestResendBackend:
    async def test_missing_api_key_falls_back_to_console_never_raises(self, monkeypatch):
        """No RESEND_API_KEY → warning logged, falls back to console, True
        returned, and — critically — no HTTP request is attempted at all
        (respx has no route registered; an attempted call would error)."""
        monkeypatch.setenv("EMAIL_BACKEND", "resend")
        fake_log = MagicMock()
        monkeypatch.setattr(email_mod, "log", fake_log)

        with respx.mock:
            ok = await email_mod.send_email(to="owner@example.com", subject="x", html="x")

        assert ok is True
        assert fake_log.warning.called
        warned_events = [call.args[0] for call in fake_log.warning.call_args_list]
        assert "email.resend.missing_api_key" in warned_events

    async def test_success_hits_resend_api(self, monkeypatch):
        monkeypatch.setenv("EMAIL_BACKEND", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "re_test_123")
        monkeypatch.setenv("EMAIL_FROM", "Mesio <notificaciones@mesio.app>")

        with respx.mock:
            route = respx.post("https://api.resend.com/emails").mock(
                return_value=Response(200, json={"id": "email_abc"})
            )
            ok = await email_mod.send_email(
                to="owner@example.com", subject="Hola", html="<p>hola</p>", text="hola"
            )

        assert ok is True
        assert route.called
        sent_payload = route.calls.last.request.content
        import json as _json
        payload = _json.loads(sent_payload)
        assert payload["to"] == ["owner@example.com"]
        assert payload["from"] == "Mesio <notificaciones@mesio.app>"
        assert payload["subject"] == "Hola"

    async def test_api_error_status_returns_false(self, monkeypatch):
        monkeypatch.setenv("EMAIL_BACKEND", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "re_test_123")

        with respx.mock:
            respx.post("https://api.resend.com/emails").mock(
                return_value=Response(422, json={"message": "invalid from address"})
            )
            ok = await email_mod.send_email(to="owner@example.com", subject="x", html="<p>x</p>")

        assert ok is False

    async def test_network_error_returns_false_not_raises(self, monkeypatch):
        """A send failure must never crash the caller — password reset still
        responds, the scheduler still moves to the next restaurant."""
        monkeypatch.setenv("EMAIL_BACKEND", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "re_test_123")

        with respx.mock:
            respx.post("https://api.resend.com/emails").mock(side_effect=httpx.ConnectError("boom"))
            ok = await email_mod.send_email(to="owner@example.com", subject="x", html="<p>x</p>")

        assert ok is False

    async def test_timeout_returns_false_not_raises(self, monkeypatch):
        monkeypatch.setenv("EMAIL_BACKEND", "resend")
        monkeypatch.setenv("RESEND_API_KEY", "re_test_123")

        with respx.mock:
            respx.post("https://api.resend.com/emails").mock(
                side_effect=httpx.TimeoutException("timed out")
            )
            ok = await email_mod.send_email(to="owner@example.com", subject="x", html="<p>x</p>")

        assert ok is False


# ══════════════════════════════════════════════════════════════════════════
# Sendgrid backend (recognized value, not implemented — no account exists)
# ══════════════════════════════════════════════════════════════════════════

class TestSendgridBackend:
    async def test_sendgrid_falls_back_to_console_with_warning(self, monkeypatch):
        monkeypatch.setenv("EMAIL_BACKEND", "sendgrid")
        fake_log = MagicMock()
        monkeypatch.setattr(email_mod, "log", fake_log)

        ok = await email_mod.send_email(to="owner@example.com", subject="x", html="x")

        assert ok is True
        assert fake_log.warning.called


# ══════════════════════════════════════════════════════════════════════════
# email_templates.py — pure rendering functions
# ══════════════════════════════════════════════════════════════════════════

class TestEmailTemplates:
    def test_password_reset_email_contains_code(self):
        subject, html, text = render_password_reset_email(code="482913", restaurant_name="El Bogotazo")
        assert "482913" in html
        assert "482913" in text
        assert "El Bogotazo" in html
        assert subject  # non-empty

    def test_password_reset_email_escapes_restaurant_name(self):
        """XSS defense-in-depth: a restaurant name with HTML must be escaped
        in the rendered body (CLAUDE.md XSS rule applies to any HTML we render,
        not just browser-side JS)."""
        subject, html, text = render_password_reset_email(
            code="111111", restaurant_name="<script>alert(1)</script>"
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_welcome_email_contains_credentials(self):
        subject, html, text = render_welcome_email(
            restaurant_name="Pizzas Don Chepe",
            username="don.chepe",
            temp_password="Xk7mPz2q",
            login_url="https://mesio.app/login",
        )
        assert "don.chepe" in html and "don.chepe" in text
        assert "Xk7mPz2q" in html and "Xk7mPz2q" in text
        assert "https://mesio.app/login" in html
        assert subject
