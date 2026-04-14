"""
tests/test_menu_image_endpoints.py

Unit tests for:
  POST /api/menu/image/sign  — returns signed Cloudinary upload params
  DELETE /api/menu/image     — deletes a Cloudinary image with ownership validation

All Cloudinary SDK calls and state_store are mocked — no real network calls.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services import database as db
from tests.conftest import patch_auth, make_row


# ─── Shared fixtures ─────────────────────────────────────────────────────────

RESTAURANT_ID = 42
AUTH_HEADERS = {"Authorization": "Bearer dummy_token"}
VALID_PUBLIC_ID = f"mesio/r_{RESTAURANT_ID}/menu/dish_abc"
OTHER_RESTAURANT_PUBLIC_ID = "mesio/r_99/menu/dish_xyz"


def _make_restaurant(restaurant_id: int = RESTAURANT_ID) -> dict:
    return {
        "id": restaurant_id,
        "name": "Restaurante Test",
        "whatsapp_number": "+573001234567",
        "features": {},
        "parent_restaurant_id": None,
    }


def _patch_restaurant_auth(monkeypatch, restaurant_id: int = RESTAURANT_ID):
    """Patch all auth deps so any Bearer token authenticates as the given restaurant."""
    restaurant = _make_restaurant(restaurant_id)
    user = {
        "username": "owner_test",
        "restaurant_name": "Restaurante Test",
        "branch_id": restaurant_id,
        "role": "owner",
        "password_hash": "$2b$12$placeholder",
    }
    monkeypatch.setattr("app.routes.deps.verify_token", AsyncMock(return_value="owner_test"))
    monkeypatch.setattr(db, "db_get_user", AsyncMock(return_value=user))
    monkeypatch.setattr(db, "db_get_restaurant_by_id", AsyncMock(return_value=restaurant))
    return restaurant


SAMPLE_SIGN_RESPONSE = {
    "signature": "abc123fakesig",
    "timestamp": 1700000000,
    "api_key": "fake_api_key",
    "cloud_name": "mesio_cloud",
    "folder": f"mesio/r_{RESTAURANT_ID}/menu",
    "public_id_prefix": f"mesio/r_{RESTAURANT_ID}/",
}


# ─── POST /api/menu/image/sign ───────────────────────────────────────────────

class TestMenuImageSign:

    def test_sign_no_auth_returns_401(self, client):
        """Request without Bearer token must return 401 (verify_token returns None)."""
        with patch("app.routes.deps.verify_token", AsyncMock(return_value=None)):
            resp = client.post("/api/menu/image/sign", json={})
        assert resp.status_code == 401

    def test_sign_with_auth_returns_200(self, client, monkeypatch):
        """Authenticated request returns 200 with valid signature dict."""
        _patch_restaurant_auth(monkeypatch)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.sign_upload_params", return_value=SAMPLE_SIGN_RESPONSE):

            resp = client.post("/api/menu/image/sign", json={}, headers=AUTH_HEADERS)

        assert resp.status_code == 200
        data = resp.json()
        assert "signature" in data
        assert "timestamp" in data
        assert "api_key" in data
        assert "cloud_name" in data
        assert "folder" in data
        assert "public_id_prefix" in data

    def test_sign_returns_correct_folder(self, client, monkeypatch):
        """Folder in response matches restaurant_id and folder_suffix."""
        _patch_restaurant_auth(monkeypatch)

        custom_response = dict(SAMPLE_SIGN_RESPONSE)
        custom_response["folder"] = f"mesio/r_{RESTAURANT_ID}/menu"

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.sign_upload_params", return_value=custom_response):

            resp = client.post(
                "/api/menu/image/sign",
                json={"folder_suffix": "menu"},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        assert resp.json()["folder"] == f"mesio/r_{RESTAURANT_ID}/menu"

    def test_sign_cloudinary_not_configured_returns_503(self, client, monkeypatch):
        """When Cloudinary env vars are missing, endpoint returns 503."""
        _patch_restaurant_auth(monkeypatch)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.sign_upload_params",
                   return_value={"error": "Cloudinary not configured — check env vars"}):

            resp = client.post("/api/menu/image/sign", json={}, headers=AUTH_HEADERS)

        assert resp.status_code == 503
        assert "configurado" in resp.json()["detail"].lower() or "soporte" in resp.json()["detail"].lower()

    def test_sign_rate_limit_31st_request_returns_429(self, client, monkeypatch):
        """31st consecutive request must return 429 (rate limit exceeded)."""
        _patch_restaurant_auth(monkeypatch)

        call_count = 0

        async def _rate_limit_side_effect(key, max_requests, window_seconds):
            nonlocal call_count
            call_count += 1
            # First 30 pass, 31st is rejected
            return call_count <= 30

        with patch("app.services.state_store.rate_limit_check",
                   side_effect=_rate_limit_side_effect), \
             patch("app.services.image_host.sign_upload_params",
                   return_value=SAMPLE_SIGN_RESPONSE):

            responses = [
                client.post("/api/menu/image/sign", json={}, headers=AUTH_HEADERS)
                for _ in range(31)
            ]

        assert all(r.status_code == 200 for r in responses[:30])
        assert responses[30].status_code == 429

    def test_sign_rate_limit_key_scoped_to_restaurant(self, client, monkeypatch):
        """Rate limit key must include restaurant_id to be scoped per tenant."""
        _patch_restaurant_auth(monkeypatch)

        captured_key = {}

        async def _capture_key(key, max_requests, window_seconds):
            captured_key["key"] = key
            return True

        with patch("app.services.state_store.rate_limit_check", side_effect=_capture_key), \
             patch("app.services.image_host.sign_upload_params",
                   return_value=SAMPLE_SIGN_RESPONSE):

            resp = client.post("/api/menu/image/sign", json={}, headers=AUTH_HEADERS)

        assert resp.status_code == 200
        assert str(RESTAURANT_ID) in captured_key["key"]


# ─── DELETE /api/menu/image ──────────────────────────────────────────────────

class TestMenuImageDelete:

    def test_delete_no_auth_returns_401(self, client):
        """Request without Bearer token must return 401 (verify_token returns None)."""
        with patch("app.routes.deps.verify_token", AsyncMock(return_value=None)):
            resp = client.request(
                "DELETE", "/api/menu/image", json={"public_id": VALID_PUBLIC_ID}
            )
        assert resp.status_code == 401

    def test_delete_own_image_returns_200(self, client, monkeypatch):
        """Deleting an image that belongs to the authenticated restaurant returns 200."""
        _patch_restaurant_auth(monkeypatch)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.delete_image", return_value=True):

            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": VALID_PUBLIC_ID},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["public_id"] == VALID_PUBLIC_ID

    def test_delete_other_restaurant_image_returns_403(self, client, monkeypatch):
        """Attempting to delete an image belonging to another restaurant returns 403."""
        _patch_restaurant_auth(monkeypatch, restaurant_id=RESTAURANT_ID)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.delete_image", return_value=False):

            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": OTHER_RESTAURANT_PUBLIC_ID},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 403
        assert "borrar" in resp.json()["detail"].lower()

    def test_delete_idempotent_first_call(self, client, monkeypatch):
        """First delete of an existing image returns 200."""
        _patch_restaurant_auth(monkeypatch)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.delete_image", return_value=True):

            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": VALID_PUBLIC_ID},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 200

    def test_delete_idempotent_second_call(self, client, monkeypatch):
        """Second delete (image already gone from Cloudinary) also returns 200."""
        _patch_restaurant_auth(monkeypatch)

        # Simulate Cloudinary returning "not found" → delete_image returns False
        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.delete_image", return_value=False):

            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": VALID_PUBLIC_ID},
                headers=AUTH_HEADERS,
            )

        # Still 200 — idempotent by design
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_delete_missing_public_id_returns_422(self, client, monkeypatch):
        """Request with empty public_id returns 422."""
        _patch_restaurant_auth(monkeypatch)

        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)):
            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": ""},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 422

    def test_delete_does_not_call_cloudinary_for_wrong_owner(self, client, monkeypatch):
        """image_host.delete_image must NOT be called when ownership check fails."""
        _patch_restaurant_auth(monkeypatch, restaurant_id=RESTAURANT_ID)

        mock_delete = MagicMock(return_value=False)
        with patch("app.services.state_store.rate_limit_check", new=AsyncMock(return_value=True)), \
             patch("app.services.image_host.delete_image", mock_delete):

            resp = client.request(
                "DELETE",
                "/api/menu/image",
                json={"public_id": OTHER_RESTAURANT_PUBLIC_ID},
                headers=AUTH_HEADERS,
            )

        assert resp.status_code == 403
        # Cloudinary should not have been called — ownership rejected before reaching it
        mock_delete.assert_not_called()
