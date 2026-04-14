"""
Catalog v2 — Fase 5d: combo_suggestions in dish shape
tests/test_catalog_v2_combos.py

Tests for normalize_dish_shape (backend) and combo_suggestions field handling.
No DB/network calls — pure unit tests.
"""
import pytest
from app.repositories.restaurant_repo import normalize_dish_shape, normalize_menu_shape


# ── normalize_dish_shape — baseline ──────────────────────────────────────────

def test_normalize_plain_dish_no_combos():
    """Legacy dish {name, description, price} should normalize without error."""
    dish = {"name": "Bandeja Paisa", "description": "Arroz y frijoles", "price": 28000}
    out = normalize_dish_shape(dish)

    assert out["name"] == "Bandeja Paisa"
    assert out["price"] == 28000
    assert out["combo_suggestions"] == []          # default must be empty list
    assert out["active"] is True
    assert out["featured"] is False
    assert out["tags"] == []
    assert out["badges"] == []
    assert out["allergens"] == []
    assert out["sort_order"] == 999


def test_normalize_full_v2_dish():
    """Full v2 dish with all fields should pass through cleanly."""
    dish = {
        "name": "Pizza Margherita",
        "description": "Tomate, mozzarella, albahaca",
        "price": 32000,
        "image_url": "https://res.cloudinary.com/mesio/image/upload/v1/mesio/r_1/dish_piz.webp",
        "image_public_id": "mesio/r_1/dish_piz",
        "tags": ["popular"],
        "badges": ["chef_pick"],
        "allergens": ["gluten", "lacteos"],
        "featured": True,
        "sort_order": 1,
        "calories": 750,
        "prep_time_min": 12,
        "active": True,
        "combo_suggestions": [
            {"dish_name": "Gaseosa Coca-Cola", "extra_price": 5000},
            {"dish_name": "Postre de la casa", "extra_price": 8000},
        ],
    }
    out = normalize_dish_shape(dish)

    assert out["combo_suggestions"] == [
        {"dish_name": "Gaseosa Coca-Cola", "extra_price": 5000},
        {"dish_name": "Postre de la casa", "extra_price": 8000},
    ]
    assert out["featured"] is True
    assert out["sort_order"] == 1
    assert out["tags"] == ["popular"]


# ── combo_suggestions validation ─────────────────────────────────────────────

def test_combo_valid_entries_pass():
    """Valid combo entries are preserved."""
    dish = {
        "name": "Hamburguesa",
        "price": 20000,
        "combo_suggestions": [
            {"dish_name": "Papas fritas", "extra_price": 4000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert len(out["combo_suggestions"]) == 1
    assert out["combo_suggestions"][0]["dish_name"] == "Papas fritas"
    assert out["combo_suggestions"][0]["extra_price"] == 4000


def test_combo_entry_missing_dish_name_discarded():
    """Entry without dish_name is silently discarded."""
    dish = {
        "name": "Tacos",
        "price": 15000,
        "combo_suggestions": [
            {"extra_price": 3000},  # missing dish_name
            {"dish_name": "Agua panela", "extra_price": 2000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert len(out["combo_suggestions"]) == 1
    assert out["combo_suggestions"][0]["dish_name"] == "Agua panela"


def test_combo_entry_empty_dish_name_discarded():
    """Entry with blank dish_name is discarded."""
    dish = {
        "name": "Ceviche",
        "price": 18000,
        "combo_suggestions": [
            {"dish_name": "   ", "extra_price": 2000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"] == []


def test_combo_entry_missing_extra_price_discarded():
    """Entry without extra_price is discarded."""
    dish = {
        "name": "Sopa",
        "price": 12000,
        "combo_suggestions": [
            {"dish_name": "Pan"},  # missing extra_price
        ],
    }
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"] == []


def test_combo_entry_negative_price_discarded():
    """Entry with negative extra_price is discarded."""
    dish = {
        "name": "Ensalada",
        "price": 15000,
        "combo_suggestions": [
            {"dish_name": "Aderezo", "extra_price": -500},
        ],
    }
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"] == []


def test_combo_non_list_treated_as_empty():
    """Non-list combo_suggestions treated as empty."""
    dish = {"name": "Arepa", "price": 5000, "combo_suggestions": "invalid"}
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"] == []


def test_combo_none_treated_as_empty():
    """None combo_suggestions treated as empty."""
    dish = {"name": "Chorizo", "price": 8000, "combo_suggestions": None}
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"] == []


def test_combo_extra_price_float_converted_to_int():
    """float extra_price (e.g. from old JSON) is stored as int."""
    dish = {
        "name": "Pollo",
        "price": 22000,
        "combo_suggestions": [
            {"dish_name": "Agua", "extra_price": 3000.0},
        ],
    }
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"][0]["extra_price"] == 3000
    assert isinstance(out["combo_suggestions"][0]["extra_price"], int)


def test_combo_mixed_valid_and_invalid_entries():
    """Mixed entries: valid ones kept, invalid silently dropped."""
    dish = {
        "name": "Bandeja",
        "price": 28000,
        "combo_suggestions": [
            {"dish_name": "Jugo de naranja", "extra_price": 4000},
            {"extra_price": 2000},                           # no dish_name
            {"dish_name": "", "extra_price": 1500},          # empty name
            {"dish_name": "Postre", "extra_price": -100},    # negative price
            {"dish_name": "Gaseosa", "extra_price": 3000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert len(out["combo_suggestions"]) == 2
    names = [c["dish_name"] for c in out["combo_suggestions"]]
    assert "Jugo de naranja" in names
    assert "Gaseosa" in names


def test_combo_non_dict_entries_discarded():
    """Non-dict items in the list are discarded."""
    dish = {
        "name": "Pasta",
        "price": 19000,
        "combo_suggestions": [
            "string_entry",
            42,
            None,
            {"dish_name": "Pan de ajo", "extra_price": 2000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert len(out["combo_suggestions"]) == 1


def test_combo_dish_name_stripped():
    """dish_name with leading/trailing spaces is stripped."""
    dish = {
        "name": "Steak",
        "price": 45000,
        "combo_suggestions": [
            {"dish_name": "  Vino tinto  ", "extra_price": 15000},
        ],
    }
    out = normalize_dish_shape(dish)
    assert out["combo_suggestions"][0]["dish_name"] == "Vino tinto"


# ── normalize_menu_shape ──────────────────────────────────────────────────────

def test_normalize_menu_shape_applies_to_all_dishes():
    """normalize_menu_shape applies normalize_dish_shape to every dish."""
    menu = {
        "Platos principales": [
            {"name": "Bandeja", "price": 28000},
            {"name": "Hamburguesa", "price": 22000, "combo_suggestions": [
                {"dish_name": "Papas", "extra_price": 3000}
            ]},
        ],
        "Bebidas": [
            {"name": "Café", "price": 3000},
        ],
    }
    out = normalize_menu_shape(menu)

    for cat, dishes in out.items():
        for dish in dishes:
            assert "combo_suggestions" in dish

    # Verify combo preserved
    hamburguesa = out["Platos principales"][1]
    assert len(hamburguesa["combo_suggestions"]) == 1
    assert hamburguesa["combo_suggestions"][0]["dish_name"] == "Papas"


def test_normalize_menu_shape_none_safe():
    """normalize_menu_shape returns empty dict for None input."""
    out = normalize_menu_shape(None)
    assert out == {}


def test_normalize_menu_shape_empty_dict():
    """normalize_menu_shape on empty menu returns empty dict."""
    out = normalize_menu_shape({})
    assert out == {}


# ── Backward compatibility: dishes without combo_suggestions modal-safe ───────

def test_backward_compat_no_combo_field():
    """Dishes from before Fase 5d (no combo_suggestions key) get default []."""
    old_dish = {"name": "Arroz con pollo", "description": "Clasico", "price": 18000}
    out = normalize_dish_shape(old_dish)
    assert out["combo_suggestions"] == []
    # Existing fields preserved
    assert out["name"] == "Arroz con pollo"
    assert out["price"] == 18000
