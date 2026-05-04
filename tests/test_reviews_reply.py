"""Tests for /api/reviews/{id}/reply WhatsApp delivery wiring (gap D).

Covers:
  - Reply persists in DB regardless of WA delivery outcome (always 200).
  - When phone+bot_number+token are present and send_text returns True,
    db_mark_review_reply_delivered is called and `delivered=True`.
  - When phone is missing, no WA send is attempted; delivered=False.
  - When send_text raises, the route still returns 200 (best-effort).
"""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import patch_auth


client = TestClient(app)


def _patch_review_ownership(monkeypatch):
    """The reply route asserts the review belongs to the caller's tenant."""
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_verify_review_ownership_by_org",
        AsyncMock(return_value=True),
    )


def test_reply_persists_and_delivers_via_whatsapp(monkeypatch):
    """Happy path: review has phone+bot_number+token → send_text fires + mark_delivered called."""
    patch_auth(monkeypatch, restaurant_id=1)
    _patch_review_ownership(monkeypatch)

    saved_review = {"id": 5, "score": 5, "comment": "great",
                    "phone": "573001234567", "bot_number": "5731234"}
    full_review_row = {**saved_review, "owner_reply": "Gracias!", "owner_reply_at": "now"}
    fake_resto = {"name": "Resto Test",
                  "wa_access_token": "token-abc",
                  "wa_phone_id":     "phone-id-xyz"}

    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_add_owner_reply",
        AsyncMock(return_value=saved_review),
    )
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_get_review",
        AsyncMock(return_value=full_review_row),
    )
    monkeypatch.setattr(
        "app.services.database.db_get_restaurant_by_phone",
        AsyncMock(return_value=fake_resto),
    )
    send_text_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.meta_api.send_text", send_text_mock)
    mark_delivered_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_mark_review_reply_delivered",
        mark_delivered_mock,
    )

    resp = client.put(
        "/api/reviews/5/reply",
        json={"reply": "Gracias por tu reseña!"},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["delivered"] is True
    send_text_mock.assert_awaited_once()
    mark_delivered_mock.assert_awaited_once_with(5)


def test_reply_returns_success_even_when_phone_missing(monkeypatch):
    """If review has no phone we still persist; delivered=False."""
    patch_auth(monkeypatch, restaurant_id=1)
    _patch_review_ownership(monkeypatch)

    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_add_owner_reply",
        AsyncMock(return_value={"id": 6, "score": 4}),
    )
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_get_review",
        AsyncMock(return_value={"id": 6, "phone": "", "bot_number": "5731234"}),
    )
    send_text_mock = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.meta_api.send_text", send_text_mock)
    mark_delivered_mock = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_mark_review_reply_delivered",
        mark_delivered_mock,
    )

    resp = client.put(
        "/api/reviews/6/reply",
        json={"reply": "Gracias!"},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200
    assert resp.json()["delivered"] is False
    send_text_mock.assert_not_awaited()
    mark_delivered_mock.assert_not_awaited()


def test_reply_returns_success_when_send_text_raises(monkeypatch):
    """send_text raising must not leak — best-effort delivery, response stays 200."""
    patch_auth(monkeypatch, restaurant_id=1)
    _patch_review_ownership(monkeypatch)

    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_add_owner_reply",
        AsyncMock(return_value={"id": 7}),
    )
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_get_review",
        AsyncMock(return_value={"id": 7, "phone": "573009998888",
                                "bot_number": "5731234"}),
    )
    monkeypatch.setattr(
        "app.services.database.db_get_restaurant_by_phone",
        AsyncMock(return_value={"name": "X", "wa_access_token": "tok",
                                "wa_phone_id": "pid"}),
    )
    monkeypatch.setattr(
        "app.services.meta_api.send_text",
        AsyncMock(side_effect=Exception("network down")),
    )
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_mark_review_reply_delivered",
        AsyncMock(),
    )

    resp = client.put(
        "/api/reviews/7/reply",
        json={"reply": "Test"},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["delivered"] is False


def test_reply_does_not_mark_delivered_when_send_text_returns_false(monkeypatch):
    """send_text returns False (e.g. Meta API rejected) → no mark_delivered call."""
    patch_auth(monkeypatch, restaurant_id=1)
    _patch_review_ownership(monkeypatch)

    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_add_owner_reply",
        AsyncMock(return_value={"id": 8}),
    )
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_get_review",
        AsyncMock(return_value={"id": 8, "phone": "573001112222",
                                "bot_number": "5731234"}),
    )
    monkeypatch.setattr(
        "app.services.database.db_get_restaurant_by_phone",
        AsyncMock(return_value={"name": "X", "wa_access_token": "tok",
                                "wa_phone_id": "pid"}),
    )
    monkeypatch.setattr(
        "app.services.meta_api.send_text",
        AsyncMock(return_value=False),
    )
    mark_delivered_mock = AsyncMock()
    monkeypatch.setattr(
        "app.repositories.reviews_repo.db_mark_review_reply_delivered",
        mark_delivered_mock,
    )

    resp = client.put(
        "/api/reviews/8/reply",
        json={"reply": "Test"},
        headers={"Authorization": "Bearer testtoken"},
    )
    assert resp.status_code == 200
    assert resp.json()["delivered"] is False
    mark_delivered_mock.assert_not_awaited()
