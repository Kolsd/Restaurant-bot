"""
test_dish_normalization.py — Unit tests for catálogo v2 dish schema helpers.

Tests cover:
  - normalize_dish_shape: defaults for legacy and extended dishes
  - validate_dish_image_ownership: multi-tenant protection
  - is_cloudinary_url: URL detection
  - db_update_menu: raises ValueError on cross-tenant images (mocked)
"""

import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch, MagicMock

from app.repositories.restaurant_repo import (
    normalize_dish_shape,
    validate_dish_image_ownership,
    _normalize_menu_dishes,
)
from app.services.image_host import is_cloudinary_url
from app.services.tenant_context import bypass_tenant_scope


# ── normalize_dish_shape ──────────────────────────────────────────────────────

class TestNormalizeDishShape:
    def test_legacy_dish_gets_all_defaults(self):
        """A legacy {name, description, price} dish gets all new fields with defaults."""
        legacy = {"name": "Bandeja Paisa", "description": "Clásico colombiano", "price": 28000}
        result = normalize_dish_shape(legacy)

        assert result["name"] == "Bandeja Paisa"
        assert result["description"] == "Clásico colombiano"
        assert result["price"] == 28000
        # New fields — all defaults
        assert result["image_url"] is None
        assert result["image_public_id"] is None
        assert result["tags"] == []
        assert result["badges"] == []
        assert result["allergens"] == []
        assert result["featured"] is False
        assert result["sort_order"] == 999
        assert result["calories"] is None
        assert result["prep_time_min"] is None
        assert result["active"] is True

    def test_legacy_dish_missing_description(self):
        """Dish without description gets empty string default."""
        result = normalize_dish_shape({"name": "Arepas", "price": 5000})
        assert result["description"] == ""

    def test_tags_preserved_on_round_trip(self):
        """Tags list survives normalize_dish_shape unchanged."""
        dish = {
            "name": "Tacos",
            "price": 12000,
            "tags": ["vegan", "gluten_free", "popular"],
        }
        result = normalize_dish_shape(dish)
        assert result["tags"] == ["vegan", "gluten_free", "popular"]

    def test_badges_preserved(self):
        result = normalize_dish_shape({"name": "X", "price": 1, "badges": ["chef_pick", "new"]})
        assert result["badges"] == ["chef_pick", "new"]

    def test_allergens_preserved(self):
        result = normalize_dish_shape({"name": "X", "price": 1, "allergens": ["gluten", "lacteos"]})
        assert result["allergens"] == ["gluten", "lacteos"]

    def test_full_extended_dish_round_trip(self):
        """A fully specified extended dish round-trips without data loss."""
        dish = {
            "name": "Bandeja Paisa",
            "description": "Plato típico",
            "price": 28000,
            "image_url": "https://res.cloudinary.com/mesio/image/upload/v1/mesio/r_42/dish_abc.webp",
            "image_public_id": "mesio/r_42/dish_abc",
            "tags": ["popular_latam"],
            "badges": ["chef_pick"],
            "allergens": ["gluten"],
            "featured": True,
            "sort_order": 3,
            "calories": 850,
            "prep_time_min": 15,
            "active": False,
        }
        result = normalize_dish_shape(dish)
        assert result["image_url"] == dish["image_url"]
        assert result["image_public_id"] == dish["image_public_id"]
        assert result["featured"] is True
        assert result["sort_order"] == 3
        assert result["calories"] == 850
        assert result["prep_time_min"] == 15
        assert result["active"] is False

    def test_decimal_price_preserved(self):
        """Decimal price is kept as-is (not coerced to float)."""
        dish = {"name": "Item", "price": Decimal("15000")}
        result = normalize_dish_shape(dish)
        assert result["price"] == Decimal("15000")

    def test_invalid_tags_type_replaced_with_empty_list(self):
        """Non-list tags field is replaced with empty list (safety net for bad data)."""
        result = normalize_dish_shape({"name": "X", "price": 1, "tags": "vegan"})
        assert result["tags"] == []

    def test_invalid_badges_type_replaced_with_empty_list(self):
        result = normalize_dish_shape({"name": "X", "price": 1, "badges": None})
        assert result["badges"] == []


# ── validate_dish_image_ownership ─────────────────────────────────────────────

class TestValidateDishImageOwnership:
    def test_no_image_returns_true(self):
        """Dish without image always passes ownership check."""
        assert validate_dish_image_ownership({}, restaurant_id=42) is True
        assert validate_dish_image_ownership({"name": "X", "price": 1}, restaurant_id=42) is True

    def test_none_image_public_id_returns_true(self):
        dish = {"name": "X", "price": 1, "image_public_id": None}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is True

    def test_empty_string_public_id_returns_true(self):
        dish = {"name": "X", "price": 1, "image_public_id": ""}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is True

    def test_correct_prefix_returns_true(self):
        dish = {"name": "X", "price": 1, "image_public_id": "mesio/r_42/dish_abc"}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is True

    def test_wrong_restaurant_returns_false(self):
        """Image from another restaurant → returns False."""
        dish = {"name": "X", "price": 1, "image_public_id": "mesio/r_99/dish_abc"}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is False

    def test_arbitrary_public_id_returns_false(self):
        """Public ID not under mesio/ prefix at all → False."""
        dish = {"name": "X", "price": 1, "image_public_id": "random/some_image"}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is False

    def test_prefix_substring_attack_blocked(self):
        """mesio/r_4/dish should NOT match restaurant_id=42."""
        dish = {"name": "X", "price": 1, "image_public_id": "mesio/r_4/dish_abc"}
        assert validate_dish_image_ownership(dish, restaurant_id=42) is False

    def test_restaurant_id_1_correct(self):
        dish = {"name": "X", "price": 1, "image_public_id": "mesio/r_1/dish_xyz"}
        assert validate_dish_image_ownership(dish, restaurant_id=1) is True

    def test_restaurant_id_1_wrong(self):
        dish = {"name": "X", "price": 1, "image_public_id": "mesio/r_10/dish_xyz"}
        assert validate_dish_image_ownership(dish, restaurant_id=1) is False


# ── is_cloudinary_url ─────────────────────────────────────────────────────────

class TestIsCloudinaryUrl:
    def test_valid_cloudinary_url_returns_true(self):
        url = "https://res.cloudinary.com/mesio/image/upload/v1/dish.webp"
        assert is_cloudinary_url(url) is True

    def test_http_cloudinary_url_returns_true(self):
        url = "http://res.cloudinary.com/mesio/image/upload/dish.jpg"
        assert is_cloudinary_url(url) is True

    def test_non_cloudinary_url_returns_false(self):
        assert is_cloudinary_url("https://example.com/dish.jpg") is False

    def test_empty_string_returns_false(self):
        assert is_cloudinary_url("") is False

    def test_none_returns_false(self):
        assert is_cloudinary_url(None) is False  # type: ignore[arg-type]

    def test_s3_url_returns_false(self):
        assert is_cloudinary_url("https://s3.amazonaws.com/bucket/dish.jpg") is False

    def test_partial_match_not_sufficient(self):
        """Domain must start with res.cloudinary.com — not just contain it."""
        assert is_cloudinary_url("https://evil.com/res.cloudinary.com/mesio/image") is False


# ── db_update_menu with cross-tenant image ────────────────────────────────────

class TestDbUpdateMenuValidation:
    @pytest.mark.asyncio
    async def test_cross_tenant_image_raises_value_error(self):
        """db_update_menu should raise ValueError if any dish has a foreign image_public_id."""
        from app.repositories.restaurant_repo import db_update_menu

        menu_data = {
            "Platos Principales": [
                {
                    "name": "Bandeja Paisa",
                    "price": 28000,
                    "image_public_id": "mesio/r_99/dish_abc",  # belongs to restaurant 99, not 42
                }
            ]
        }

        # db_update_menu is now tenant-scoped — ValueError raised before DB call
        # so we only need bypass + pool mock; the error happens in validation.
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=txn)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.database.get_pool", AsyncMock(return_value=mock_pool)):
            with bypass_tenant_scope("test_cross_tenant_image"):
                with pytest.raises(ValueError, match="mesio/r_99/dish_abc"):
                    await db_update_menu(restaurant_id=42, menu_data=menu_data)

    @pytest.mark.asyncio
    async def test_valid_menu_calls_db(self):
        """db_update_menu with valid images proceeds to DB execute."""
        from app.repositories.restaurant_repo import db_update_menu

        menu_data = {
            "Platos": [
                {
                    "name": "Arepa",
                    "price": 5000,
                    "image_public_id": "mesio/r_42/arepa",  # correct owner
                }
            ]
        }

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=txn)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.database.get_pool", AsyncMock(return_value=mock_pool)):
            with bypass_tenant_scope("test_valid_menu"):
                result = await db_update_menu(restaurant_id=42, menu_data=menu_data)

        assert result is True
        # execute is called at least once for the UPDATE (bypass mode also calls SET LOCAL ROLE)
        assert mock_conn.execute.call_count >= 1
        update_calls = [
            c for c in mock_conn.execute.call_args_list
            if c.args and "UPDATE restaurants SET menu" in str(c.args[0])
        ]
        assert len(update_calls) == 1

    @pytest.mark.asyncio
    async def test_legacy_menu_no_images_passes(self):
        """Legacy menu without any image fields passes validation and is normalized."""
        from app.repositories.restaurant_repo import db_update_menu

        menu_data = {
            "Bebidas": [
                {"name": "Café", "price": 3000, "description": "Tinto"},
            ]
        }

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")
        txn = MagicMock()
        txn.__aenter__ = AsyncMock(return_value=txn)
        txn.__aexit__ = AsyncMock(return_value=False)
        mock_conn.transaction = MagicMock(return_value=txn)
        mock_conn.fetchval = AsyncMock(return_value=None)
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.database.get_pool", AsyncMock(return_value=mock_pool)):
            with bypass_tenant_scope("test_legacy_menu"):
                result = await db_update_menu(restaurant_id=1, menu_data=menu_data)

        assert result is True
        # The normalized dish was passed to execute
        import json
        call_args = mock_conn.execute.call_args
        saved_menu = json.loads(call_args[0][1])  # second positional arg = json.dumps(menu_data)
        saved_dish = saved_menu["Bebidas"][0]
        assert saved_dish["tags"] == []
        assert saved_dish["active"] is True
        assert saved_dish["sort_order"] == 999


# ── _normalize_menu_dishes ────────────────────────────────────────────────────

class TestNormalizeMenuDishes:
    def test_non_dict_menu_returned_unchanged(self):
        """Non-dict menus (corrupted data) are returned as-is without crashing."""
        assert _normalize_menu_dishes(None) is None  # type: ignore[arg-type]
        assert _normalize_menu_dishes([]) == []  # type: ignore[arg-type]

    def test_empty_menu(self):
        assert _normalize_menu_dishes({}) == {}

    def test_categories_without_lists_preserved(self):
        """Categories with non-list values (corrupted) are kept as-is."""
        menu = {"Bad": "not a list"}
        result = _normalize_menu_dishes(menu)
        assert result["Bad"] == "not a list"

    def test_normalizes_all_dishes_in_all_categories(self):
        menu = {
            "Cat A": [{"name": "Dish 1", "price": 1000}],
            "Cat B": [{"name": "Dish 2", "price": 2000}, {"name": "Dish 3", "price": 3000}],
        }
        result = _normalize_menu_dishes(menu)
        for cat, dishes in result.items():
            for dish in dishes:
                assert "tags" in dish
                assert "active" in dish
                assert "image_url" in dish
