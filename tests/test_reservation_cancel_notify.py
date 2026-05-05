"""
tests/test_reservation_cancel_notify.py — Polish batch GAP C.

Covers PUT /api/reservations/{id}/status with status='cancelled':
  - sends a WhatsApp text to the customer (best effort, fire-and-forget)
  - skips silently when reservation has no phone
  - skips silently when restaurant has no WA token
  - update still persists when the send raises
  - non-cancel status updates do NOT trigger a send

No live DB or HTTP — meta_api.send_text is mocked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import patch_auth


FUTURE_DATE = (datetime.now(tz=timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")


def _sample_reservation(*, phone: str = "+573001234567", bot_number: str = "+573009998877"):
    return {
        "id":           42,
        "org_id":       1,
        "name":         "Juan Pérez",
        "date":         FUTURE_DATE,
        "time":         "20:00",
        "guests":       4,
        "phone":        phone,
        "notes":        "",
        "table_id":     None,
        "source":       "manual",
        "status":       "cancelled",
        "bot_number":   bot_number,
        "cancelled_at": "2026-04-22T10:00:00",
        "cancellation_reason": "fuga de gas",
    }


def _setup(monkeypatch, *, wa_token: str = "test_token", phone_id: str = "12345"):
    """Patch auth + reservation lookup. Returns (client, restaurant)."""
    from app.services import database as db
    from app.repositories import reservations_repo

    restaurant = patch_auth(
        monkeypatch,
        restaurant_id=1,
        whatsapp_number="+573009998877",
        features={"module_reservations": True},
    )
    restaurant["wa_access_token"] = wa_token
    restaurant["wa_phone_id"] = phone_id
    restaurant["name"] = "El Trompo"

    # The router fetches the reservation twice: once for ownership check via
    # db_get_reservation_by_id, once for the actual cancel via db_cancel_reservation.
    monkeypatch.setattr(
        db, "db_get_reservation_by_id",
        AsyncMock(return_value=_sample_reservation()),
    )
    monkeypatch.setattr(
        db, "db_cancel_reservation",
        AsyncMock(return_value=_sample_reservation()),
    )
    monkeypatch.setattr(
        db, "db_confirm_reservation",
        AsyncMock(return_value={**_sample_reservation(), "status": "confirmed"}),
    )
    monkeypatch.setattr(
        db, "db_mark_no_show",
        AsyncMock(return_value={**_sample_reservation(), "status": "no_show"}),
    )
    monkeypatch.setattr(
        db, "db_update_reservation_status",
        AsyncMock(return_value={**_sample_reservation(), "status": "completed"}),
    )

    from app.main import app
    return TestClient(app), restaurant


def test_admin_cancel_sends_whatsapp_to_customer(monkeypatch):
    """status='cancelled' with phone+token → meta_api.send_text invoked once."""
    client, _ = _setup(monkeypatch)

    send_mock = AsyncMock(return_value=True)
    with patch("app.services.meta_api.send_text", send_mock):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "cancelled", "reason": "incendio cocina"},
            headers={"Authorization": "Bearer t"},
        )

    assert resp.status_code == 200, resp.text
    send_mock.assert_awaited_once()
    # Inspect the message body for shape
    args, kwargs = send_mock.await_args
    # signature: send_text(bot_number, token, phone, text, phone_id=...)
    assert args[2] == "+573001234567"   # phone
    assert "cancelar" in args[3].lower()
    assert "incendio cocina" in args[3]


def test_admin_cancel_persists_even_when_send_fails(monkeypatch):
    """If meta_api.send_text raises, the API response is still 200 with the cancelled row."""
    client, _ = _setup(monkeypatch)

    async def _boom(*a, **kw):
        raise RuntimeError("Meta API unreachable")

    with patch("app.services.meta_api.send_text", _boom):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "cancelled", "reason": "test"},
            headers={"Authorization": "Bearer t"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "cancelled"


def test_admin_cancel_skips_send_when_no_phone(monkeypatch):
    """Reservation without phone → no send attempted, response still 200."""
    from app.services import database as db

    client, _ = _setup(monkeypatch)
    # Override the cancel return to have empty phone
    monkeypatch.setattr(
        db, "db_cancel_reservation",
        AsyncMock(return_value=_sample_reservation(phone="")),
    )

    send_mock = AsyncMock(return_value=True)
    with patch("app.services.meta_api.send_text", send_mock):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "cancelled"},
            headers={"Authorization": "Bearer t"},
        )

    assert resp.status_code == 200
    send_mock.assert_not_awaited()


def test_admin_cancel_skips_send_when_no_wa_token(monkeypatch):
    """Restaurant without wa_access_token → no send attempted, response still 200."""
    client, _ = _setup(monkeypatch, wa_token="")

    send_mock = AsyncMock(return_value=True)
    with patch("app.services.meta_api.send_text", send_mock):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "cancelled"},
            headers={"Authorization": "Bearer t"},
        )

    assert resp.status_code == 200
    send_mock.assert_not_awaited()


def test_confirm_does_not_trigger_send(monkeypatch):
    """status='confirmed' must NOT call send_text — only cancellations notify."""
    client, _ = _setup(monkeypatch)

    send_mock = AsyncMock(return_value=True)
    with patch("app.services.meta_api.send_text", send_mock):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "confirmed"},
            headers={"Authorization": "Bearer t"},
        )

    assert resp.status_code == 200
    send_mock.assert_not_awaited()


def test_no_show_does_not_trigger_send(monkeypatch):
    """status='no_show' (post-hoc admin marking) must NOT notify the customer."""
    client, _ = _setup(monkeypatch)

    send_mock = AsyncMock(return_value=True)
    with patch("app.services.meta_api.send_text", send_mock):
        resp = client.put(
            "/api/reservations/42/status",
            json={"status": "no_show"},
            headers={"Authorization": "Bearer t"},
        )

    assert resp.status_code == 200
    send_mock.assert_not_awaited()
