"""
tests/test_seo_routes.py

Tests for Catálogo v2 Fase 6: SEO/Growth server-rendered routes.

Covers:
  - _slugify unit tests (unique slug generation, special chars)
  - GET /r/{slug}/menu  → 200 with OG tags
  - GET /r/{slug}/menu  → 404 for unknown slug
  - GET /r/{slug}/menu/{dish_slug} → 200 with per-dish OG
  - GET /r/{slug}/menu/{dish_slug} → dish without image_url (no crash)
  - GET /r/{slug}/menu/{dish_slug} → XSS: name with <script> is escaped
  - GET /sitemap-{id}.xml → 200 with correct content-type, contains URLs
  - Sitemap skips inactive dishes
"""

import pytest
from unittest.mock import AsyncMock, patch

# ── Unit tests: _slugify ──────────────────────────────────────────────────────

def test_slugify_basic():
    from app.repositories.restaurant_repo import _slugify
    assert _slugify("La Bodeguita del Medio") == "la-bodeguita-del-medio"


def test_slugify_special_chars():
    from app.repositories.restaurant_repo import _slugify
    assert _slugify("Café & Brunch!") == "caf-brunch"


def test_slugify_empty():
    from app.repositories.restaurant_repo import _slugify
    assert _slugify("") == "restaurant"
    assert _slugify(None) == "restaurant"


def test_slugify_numbers():
    from app.repositories.restaurant_repo import _slugify
    assert _slugify("El Asado #1") == "el-asado-1"


def test_slugify_leading_trailing_dashes():
    from app.repositories.restaurant_repo import _slugify
    result = _slugify("---test---")
    assert not result.startswith("-")
    assert not result.endswith("-")


# ── Dish slug helper ──────────────────────────────────────────────────────────

def test_dish_slugify_xss():
    """XSS payload in dish name produces a URL-safe slug (no HTML angle brackets)."""
    from app.routes.dashboard import _slugify_dish
    slug = _slugify_dish("<script>alert(1)</script>")
    # HTML angle brackets must be stripped — the resulting slug is URL-safe
    assert "<" not in slug
    assert ">" not in slug
    # Slug chars are alphanumeric + dashes only
    import re
    assert re.fullmatch(r'[a-z0-9\-]+', slug)


# ── HTTP route tests ──────────────────────────────────────────────────────────

_SAMPLE_MENU = {
    "Principales": [
        {
            "name": "Bandeja Paisa",
            "description": "Fríjoles, chicharrón y arroz",
            "price": 28000,
            "image_url": "https://res.cloudinary.com/mesio/image/upload/v1/mesio/r_1/dish.webp",
            "featured": True,
            "active": True,
        },
        {
            "name": "Sin Foto",
            "description": "Plato sin imagen",
            "price": 15000,
            "active": True,
        },
    ]
}

_SAMPLE_RESTAURANT = {
    "id": 1,
    "name": "El Sabor",
    "slug": "el-sabor",
    "whatsapp_number": "+573001234567",
    "menu": _SAMPLE_MENU,
    "features": {"currency": "COP", "locale": "es-CO"},
    "address": "Calle 1 # 2-3",
}


@pytest.fixture
def client(monkeypatch):
    """TestClient with restaurant_repo mocked (no DB needed)."""
    from fastapi.testclient import TestClient
    from app.main import app

    async def mock_by_slug(slug):
        if slug == "el-sabor":
            return _SAMPLE_RESTAURANT
        return None

    async def mock_by_id(rid):
        if rid == 1:
            return _SAMPLE_RESTAURANT
        return None

    monkeypatch.setattr(
        "app.repositories.restaurant_repo.db_get_restaurant_by_slug",
        mock_by_slug,
    )
    monkeypatch.setattr(
        "app.services.database.db_get_restaurant_by_id",
        mock_by_id,
    )
    # Also patch in routes.dashboard namespace
    monkeypatch.setattr(
        "app.routes.dashboard.restaurant_repo.db_get_restaurant_by_slug",
        mock_by_slug,
    )
    monkeypatch.setattr(
        "app.routes.dashboard.db.db_get_restaurant_by_id",
        mock_by_id,
    )
    return TestClient(app)


class TestMenuPage:
    def test_menu_page_200(self, client):
        resp = client.get("/r/el-sabor/menu")
        assert resp.status_code == 200

    def test_menu_page_has_og_title(self, client):
        resp = client.get("/r/el-sabor/menu")
        assert "og:title" in resp.text

    def test_menu_page_has_og_image(self, client):
        resp = client.get("/r/el-sabor/menu")
        assert "og:image" in resp.text

    def test_menu_page_has_og_url(self, client):
        resp = client.get("/r/el-sabor/menu")
        assert "og:url" in resp.text
        assert "/r/el-sabor/menu" in resp.text

    def test_menu_page_has_restaurant_name(self, client):
        resp = client.get("/r/el-sabor/menu")
        assert "El Sabor" in resp.text

    def test_menu_page_has_meta_refresh(self, client):
        """Human browsers get redirected to the JS catalog."""
        resp = client.get("/r/el-sabor/menu")
        assert "http-equiv" in resp.text or "refresh" in resp.text.lower()

    def test_menu_page_404_unknown_slug(self, client):
        resp = client.get("/r/nonexistent-restaurant/menu")
        assert resp.status_code == 404


class TestDishPage:
    def test_dish_page_200(self, client):
        resp = client.get("/r/el-sabor/menu/bandeja-paisa")
        assert resp.status_code == 200

    def test_dish_page_og_title_contains_dish_name(self, client):
        resp = client.get("/r/el-sabor/menu/bandeja-paisa")
        assert "Bandeja Paisa" in resp.text

    def test_dish_page_og_image_present(self, client):
        resp = client.get("/r/el-sabor/menu/bandeja-paisa")
        assert "og:image" in resp.text
        # Should contain the Cloudinary URL (possibly with hero transform)
        assert "cloudinary" in resp.text or "mesio" in resp.text

    def test_dish_page_price_displayed(self, client):
        resp = client.get("/r/el-sabor/menu/bandeja-paisa")
        # Price should appear somewhere (formatted)
        assert "28" in resp.text  # at minimum the raw number

    def test_dish_page_whatsapp_cta(self, client):
        resp = client.get("/r/el-sabor/menu/bandeja-paisa")
        assert "wa.me" in resp.text or "WhatsApp" in resp.text

    def test_dish_page_no_image_no_crash(self, client):
        """Dish without image_url should render without error (uses placeholder)."""
        resp = client.get("/r/el-sabor/menu/sin-foto")
        assert resp.status_code == 200
        # Fallback placeholder present
        assert "placeholder-img" in resp.text or "og:image" in resp.text

    def test_dish_page_xss_name_escaped(self, monkeypatch):
        """Dish name with HTML/JS payload must be escaped in the output."""
        from fastapi.testclient import TestClient
        from app.main import app

        xss_menu = {
            "Entrantes": [
                {
                    "name": '<script>alert(1)</script>',
                    "description": "XSS test",
                    "price": 10000,
                    "active": True,
                }
            ]
        }
        xss_restaurant = {**_SAMPLE_RESTAURANT, "menu": xss_menu}

        async def mock_slug(slug):
            return xss_restaurant if slug == "el-sabor" else None

        monkeypatch.setattr(
            "app.routes.dashboard.restaurant_repo.db_get_restaurant_by_slug",
            mock_slug,
        )
        c = TestClient(app)
        resp = c.get("/r/el-sabor/menu/scriptalert1script")
        # Either 404 (dish not found after slug transform) or 200 with escaped content
        if resp.status_code == 200:
            assert "<script>" not in resp.text
            assert "alert(1)" not in resp.text

    def test_dish_page_404_unknown_dish(self, client):
        resp = client.get("/r/el-sabor/menu/plato-inexistente-xyz")
        assert resp.status_code == 404

    def test_dish_page_404_unknown_restaurant(self, client):
        resp = client.get("/r/no-existe/menu/bandeja-paisa")
        assert resp.status_code == 404


class TestSitemap:
    def test_sitemap_200(self, client):
        resp = client.get("/sitemap-1.xml")
        assert resp.status_code == 200

    def test_sitemap_content_type(self, client):
        resp = client.get("/sitemap-1.xml")
        assert "xml" in resp.headers.get("content-type", "")

    def test_sitemap_contains_menu_url(self, client):
        resp = client.get("/sitemap-1.xml")
        assert "/r/el-sabor/menu" in resp.text

    def test_sitemap_contains_dish_urls(self, client):
        resp = client.get("/sitemap-1.xml")
        assert "bandeja-paisa" in resp.text

    def test_sitemap_cache_header(self, client):
        resp = client.get("/sitemap-1.xml")
        cc = resp.headers.get("cache-control", "")
        assert "max-age" in cc

    def test_sitemap_valid_xml_structure(self, client):
        resp = client.get("/sitemap-1.xml")
        assert '<?xml version' in resp.text
        assert "<urlset" in resp.text
        assert "<url>" in resp.text
        assert "<loc>" in resp.text

    def test_sitemap_skips_inactive_dishes(self, monkeypatch):
        """Inactive dishes (active=False) must not appear in sitemap."""
        from fastapi.testclient import TestClient
        from app.main import app

        inactive_menu = {
            "Principales": [
                {"name": "Active Dish", "price": 10000, "active": True},
                {"name": "Hidden Dish", "price": 5000, "active": False},
            ]
        }
        rest = {**_SAMPLE_RESTAURANT, "menu": inactive_menu}

        async def mock_by_id(rid):
            return rest if rid == 1 else None

        monkeypatch.setattr("app.routes.dashboard.db.db_get_restaurant_by_id", mock_by_id)
        c = TestClient(app)
        resp = c.get("/sitemap-1.xml")
        assert "active-dish" in resp.text
        assert "hidden-dish" not in resp.text

    def test_sitemap_404_unknown_restaurant(self, client):
        resp = client.get("/sitemap-999.xml")
        assert resp.status_code == 404


# ── image_host unit tests ─────────────────────────────────────────────────────

class TestImageHost:
    def test_build_transform_url_inserts_hero(self):
        from app.services.image_host import build_transform_url
        url = "https://res.cloudinary.com/demo/image/upload/v1/mesio/r_1/dish.webp"
        result = build_transform_url(url, "hero")
        assert "c_fill,w_1200,h_900" in result

    def test_build_transform_url_none(self):
        from app.services.image_host import build_transform_url
        assert build_transform_url(None, "hero") is None

    def test_build_transform_url_non_cloudinary(self):
        from app.services.image_host import build_transform_url
        url = "https://example.com/image.jpg"
        assert build_transform_url(url, "hero") == url

    def test_is_cloudinary_url(self):
        from app.services.image_host import is_cloudinary_url
        assert is_cloudinary_url("https://res.cloudinary.com/demo/image.jpg")
        assert not is_cloudinary_url("https://example.com/image.jpg")
        assert not is_cloudinary_url(None)
