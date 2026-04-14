"""
tests/test_catalog_v2_public_api.py

Tests for catalog v2 public API additions:
  - Tarea 1: feature flags in GET /api/public/menu/{bot_number}
             and GET /api/public/menu-context/{table_id}
  - Tarea 2: conditional HTML routing via catalog_v2_enabled flag
  - Tarea 3: POST /api/public/menu/track — validation, rate limiting, logging
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.services import database as db, state_store


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _make_menu_data(features: dict = None, *, name="Test Restaurant"):
    return {
        "name": name,
        "menu": {"Platos": [{"name": "Bandeja", "price": 28000}]},
        "parent_menu": None,
        "availability": {},
        "features": features or {},
        "parent_features": {},
    }


def _make_table(restaurant_features: dict = None):
    table = {"id": "tbl-1", "name": "Mesa 1", "restaurant_id": 1}
    restaurant = {
        "id": 1,
        "name": "Test Restaurant",
        "whatsapp_number": "573001234567",
        "features": restaurant_features or {},
    }
    return table, restaurant


@pytest.fixture
def client():
    return TestClient(app)


# ─── Tarea 1: GET /api/public/menu/{bot_number} ───────────────────────────────

class TestPublicMenuFeatureFlags:
    def test_defaults_when_features_empty(self, client, monkeypatch):
        data = _make_menu_data(features={})
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_public_menu_data",
            AsyncMock(return_value=data),
        )
        resp = client.get("/api/public/menu/573001234567")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalog_v2_enabled"] is True   # default
        assert body["bot_visual_menu"] is False      # default
        assert body["locale"] == "es-CO"
        assert body["currency"] == "COP"
        assert body["restaurant_name"] == "Test Restaurant"
        assert body["bot_number"] == "573001234567"

    def test_respects_explicit_flags(self, client, monkeypatch):
        data = _make_menu_data(features={
            "catalog_v2_enabled": False,
            "bot_visual_menu": True,
            "locale": "es-CO",
            "currency": "COP",
        })
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_public_menu_data",
            AsyncMock(return_value=data),
        )
        resp = client.get("/api/public/menu/573001234567")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalog_v2_enabled"] is False
        assert body["bot_visual_menu"] is True
        assert body["locale"] == "es-CO"
        assert body["currency"] == "COP"

    def test_404_when_not_found(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.repositories.restaurant_repo.db_get_public_menu_data",
            AsyncMock(return_value=None),
        )
        resp = client.get("/api/public/menu/000000")
        assert resp.status_code == 404


# ─── Tarea 1: GET /api/public/menu-context/{table_id} ────────────────────────

class TestPublicMenuContextFeatureFlags:
    def _patch_context(self, monkeypatch, features: dict = None):
        table, restaurant = _make_table(features)
        monkeypatch.setattr(db, "db_get_table_by_id", AsyncMock(return_value=table))
        monkeypatch.setattr(db, "db_get_menu", AsyncMock(return_value={}))
        monkeypatch.setattr(db, "db_get_menu_availability", AsyncMock(return_value={}))
        monkeypatch.setattr(
            db, "db_get_restaurant_by_bot_number", AsyncMock(return_value=restaurant)
        )
        # get_table_wa_number resolves via restaurant whatsapp_number
        monkeypatch.setattr(
            "app.routes.tables.get_table_wa_number",
            AsyncMock(return_value="573001234567"),
        )

    def test_defaults_when_features_empty(self, client, monkeypatch):
        self._patch_context(monkeypatch, features={})
        resp = client.get("/api/public/menu-context/tbl-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalog_v2_enabled"] is True
        assert body["bot_visual_menu"] is False
        assert body["bot_number"] == "573001234567"

    def test_respects_explicit_flags(self, client, monkeypatch):
        self._patch_context(monkeypatch, features={
            "catalog_v2_enabled": False,
            "bot_visual_menu": True,
            "locale": "es-CO",
            "currency": "COP",
        })
        resp = client.get("/api/public/menu-context/tbl-1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalog_v2_enabled"] is False
        assert body["bot_visual_menu"] is True
        assert body["locale"] == "es-CO"


# ─── Tarea 2: /menu/{table_id} HTML routing ───────────────────────────────────

class TestMenuHtmlRouting:
    def _patch_html_route(self, monkeypatch, catalog_v2_enabled: bool):
        table, restaurant = _make_table({"catalog_v2_enabled": catalog_v2_enabled})
        monkeypatch.setattr(db, "db_get_table_by_id", AsyncMock(return_value=table))
        monkeypatch.setattr(
            db, "db_get_restaurant_by_bot_number", AsyncMock(return_value=restaurant)
        )
        monkeypatch.setattr(
            "app.routes.tables.get_table_wa_number",
            AsyncMock(return_value="573001234567"),
        )

    def test_serves_menu_html_when_v2_enabled(self, client, monkeypatch):
        self._patch_html_route(monkeypatch, catalog_v2_enabled=True)
        resp = client.get("/menu/tbl-1")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")

    def test_serves_legacy_html_when_v2_disabled(self, client, monkeypatch, tmp_path):
        self._patch_html_route(monkeypatch, catalog_v2_enabled=False)
        # Patch STATIC to tmp dir with a stub menu-legacy.html
        legacy = tmp_path / "html"
        legacy.mkdir()
        (legacy / "menu.html").write_text("<html>new</html>")
        (legacy / "menu-legacy.html").write_text("<html>legacy</html>")
        monkeypatch.setattr("app.routes.tables.STATIC", tmp_path)
        resp = client.get("/menu/tbl-1")
        assert resp.status_code == 200
        assert "legacy" in resp.text

    def test_serves_new_html_when_v2_enabled_explicit(self, client, monkeypatch, tmp_path):
        self._patch_html_route(monkeypatch, catalog_v2_enabled=True)
        legacy = tmp_path / "html"
        legacy.mkdir()
        (legacy / "menu.html").write_text("<html>new</html>")
        (legacy / "menu-legacy.html").write_text("<html>legacy</html>")
        monkeypatch.setattr("app.routes.tables.STATIC", tmp_path)
        resp = client.get("/menu/tbl-1")
        assert resp.status_code == 200
        assert "new" in resp.text


# ─── Tarea 3: POST /api/public/menu/track ─────────────────────────────────────

class TestMenuTrack:
    def test_valid_event_returns_ok(self, client):
        resp = client.post("/api/public/menu/track", json={
            "dish_name": "Bandeja Paisa",
            "event_type": "view",
            "bot_number": "573001234567",
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_all_valid_event_types(self, client):
        for evt in ("view", "modal_open", "add_to_cart"):
            resp = client.post("/api/public/menu/track", json={
                "dish_name": "Plato",
                "event_type": evt,
                "bot_number": "57300",
            })
            assert resp.status_code == 200, f"Failed for event_type={evt}"

    def test_invalid_event_type_rejected(self, client):
        resp = client.post("/api/public/menu/track", json={
            "dish_name": "Bandeja",
            "event_type": "unknown_event",
            "bot_number": "573001234567",
        })
        assert resp.status_code == 422

    def test_dish_name_too_long_rejected(self, client):
        resp = client.post("/api/public/menu/track", json={
            "dish_name": "A" * 201,
            "event_type": "view",
            "bot_number": "573001234567",
        })
        assert resp.status_code == 422

    def test_bot_number_too_long_rejected(self, client):
        resp = client.post("/api/public/menu/track", json={
            "dish_name": "Plato",
            "event_type": "view",
            "bot_number": "1" * 21,
        })
        assert resp.status_code == 422

    def test_rate_limit_still_returns_ok(self, client, monkeypatch):
        """Even when rate-limited, returns 200 (sendBeacon compatibility)."""
        monkeypatch.setattr(
            state_store, "rate_limit_check", AsyncMock(return_value=False)
        )
        resp = client.post("/api/public/menu/track", json={
            "dish_name": "Bandeja",
            "event_type": "view",
            "bot_number": "573001234567",
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_structlog_called(self, client, monkeypatch):
        """Verify structlog.info is called with catalog.track on a valid event."""
        logged = []

        async def fake_rate_limit(*args, **kwargs):
            return True

        monkeypatch.setattr(state_store, "rate_limit_check", fake_rate_limit)

        import app.routes.dashboard as dash_mod
        original_log = dash_mod.log

        class _CapLog:
            def info(self, event, **kw):
                logged.append({"event": event, **kw})
            def warning(self, event, **kw):
                pass
            def error(self, event, **kw):
                pass

        monkeypatch.setattr(dash_mod, "log", _CapLog())

        resp = client.post("/api/public/menu/track", json={
            "dish_name": "Bandeja Paisa",
            "event_type": "add_to_cart",
            "bot_number": "573001234567",
        })
        assert resp.status_code == 200
        assert any(e["event"] == "catalog.track" for e in logged)
        monkeypatch.setattr(dash_mod, "log", original_log)
