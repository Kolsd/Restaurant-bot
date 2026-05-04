"""
tests/test_menu_missing_photos.py

Test suite for GET /api/menu/missing-photos — the visual-onboarding gap report
used by the admin menu editor to identify dishes without uploaded images.

The handler is a pure-Python projection over the restaurant dict's `menu` field,
so these tests exercise it directly without spinning up the FastAPI HTTP stack.
This mirrors the existing pattern in tests/test_bot_visual_menu.py (direct
function invocation via asyncio.run).

Tests:
  1. test_endpoint_returns_zero_when_all_dishes_have_image
  2. test_endpoint_returns_dishes_without_image_url
  3. test_endpoint_handles_empty_menu
  4. test_endpoint_handles_string_json_menu
  5. test_endpoint_tenant_isolated
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.routes.settings_routes import get_dishes_missing_photos


def _run(coro):
    """Same idiom as tests/test_bot_visual_menu.py — keep style consistent."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _restaurant(menu, rid: int = 1, whatsapp_number: str = "+573000000001"):
    """Build a minimal restaurant dict — only the keys the handler reads."""
    return {
        "id":              rid,
        "whatsapp_number": whatsapp_number,
        "menu":            menu,
    }


# ── Test 1 — full coverage: missing_count == 0 ───────────────────────────────


def test_endpoint_returns_zero_when_all_dishes_have_image():
    """All 3 dishes have image_url set → missing_count = 0, missing list empty."""
    menu = {
        "Entradas": [
            {"name": "Patacón", "price": 8000, "image_url": "https://res.cloudinary.com/m/r_1/p.webp"},
        ],
        "Platos fuertes": [
            {"name": "Bandeja Paisa", "price": 28000, "image_url": "https://res.cloudinary.com/m/r_1/b.webp"},
            {"name": "Sancocho",      "price": 22000, "image_url": "https://res.cloudinary.com/m/r_1/s.webp"},
        ],
    }

    result = _run(get_dishes_missing_photos(restaurant=_restaurant(menu)))

    assert result["total_dishes"] == 3
    assert result["with_photo"] == 3
    assert result["missing_count"] == 0
    assert result["missing"] == []


# ── Test 2 — partial coverage: returns the missing dishes correctly ──────────


def test_endpoint_returns_dishes_without_image_url():
    """5 dishes total, 3 lack image_url → missing_count = 3, names + categories returned."""
    menu = {
        "Entradas": [
            {"name": "Patacón", "price": 8000, "image_url": "https://res.cloudinary.com/m/r_1/p.webp"},
            {"name": "Empanada",  "price": 4000, "image_url": None},
        ],
        "Platos fuertes": [
            {"name": "Bandeja Paisa", "price": 28000, "image_url": ""},      # empty string counts as missing
            {"name": "Ajiaco",        "price": 25000},                        # key absent → missing
            {"name": "Sancocho",      "price": 22000, "image_url": "https://res.cloudinary.com/m/r_1/s.webp"},
        ],
    }

    result = _run(get_dishes_missing_photos(restaurant=_restaurant(menu)))

    assert result["total_dishes"] == 5
    assert result["with_photo"] == 2
    assert result["missing_count"] == 3

    names = sorted(m["name"] for m in result["missing"])
    assert names == ["Ajiaco", "Bandeja Paisa", "Empanada"]

    # Category attribution is correct for each missing dish.
    by_name = {m["name"]: m for m in result["missing"]}
    assert by_name["Empanada"]["category"] == "Entradas"
    assert by_name["Bandeja Paisa"]["category"] == "Platos fuertes"
    assert by_name["Ajiaco"]["category"] == "Platos fuertes"

    # Prices are propagated for the admin to identify the dish.
    assert by_name["Empanada"]["price"] == 4000
    assert by_name["Ajiaco"]["price"] == 25000


# ── Test 3 — empty menu: no 500, returns clean shape ─────────────────────────


def test_endpoint_handles_empty_menu():
    """menu = {} (and no whatsapp_number fallback target) → zeros, empty list, NO 500."""
    # whatsapp_number=None disables the fallback DB lookup so we don't have to
    # mock db_get_menu in this pure-shape test.
    rest = {"id": 1, "whatsapp_number": None, "menu": {}}

    result = _run(get_dishes_missing_photos(restaurant=rest))

    assert result["total_dishes"] == 0
    assert result["with_photo"] == 0
    assert result["missing_count"] == 0
    assert result["missing"] == []


# ── Test 4 — DB returns menu as raw JSON string ──────────────────────────────


def test_endpoint_handles_string_json_menu():
    """When restaurant.menu is a str (raw JSONB from some code paths), the handler parses it."""
    menu_dict = {
        "Postres": [
            {"name": "Brownie", "price": 9000, "image_url": "https://res.cloudinary.com/m/r_1/b.webp"},
            {"name": "Helado",  "price": 7000},  # no image_url
        ],
    }
    menu_str = json.dumps(menu_dict)

    result = _run(get_dishes_missing_photos(restaurant=_restaurant(menu_str)))

    assert result["total_dishes"] == 2
    assert result["with_photo"] == 1
    assert result["missing_count"] == 1
    assert result["missing"][0]["name"] == "Helado"
    assert result["missing"][0]["category"] == "Postres"


# ── Test 5 — tenant isolation: org A's response only reflects org A's menu ───


def test_endpoint_tenant_isolated():
    """The handler reads ONLY from the supplied restaurant dict; org B's menu never leaks.

    This is structural rather than RLS-based — the dependency
    get_current_restaurant resolves the restaurant for the authenticated org,
    and the handler never queries beyond that dict (no DB call when menu is
    populated). So if org A's menu has 2 dishes (1 missing photo) and org B's
    menu is independent, calling the handler for each must return their own
    aggregate, never a cross-pollinated count.
    """
    menu_a = {
        "Tapas": [
            {"name": "Aceitunas", "price": 5000, "image_url": "https://res.cloudinary.com/m/r_10/a.webp"},
            {"name": "Croquetas", "price": 9000},  # missing
        ],
    }
    menu_b = {
        "Tapas": [
            {"name": "Tortilla", "price": 12000},   # missing
            {"name": "Pulpo",    "price": 22000},   # missing
            {"name": "Jamón",    "price": 18000, "image_url": "https://res.cloudinary.com/m/r_20/j.webp"},
        ],
    }

    rest_a = _restaurant(menu_a, rid=10, whatsapp_number="+573000000010")
    rest_b = _restaurant(menu_b, rid=20, whatsapp_number="+573000000020")

    res_a = _run(get_dishes_missing_photos(restaurant=rest_a))
    res_b = _run(get_dishes_missing_photos(restaurant=rest_b))

    # Org A — independent count
    assert res_a["total_dishes"] == 2
    assert res_a["missing_count"] == 1
    assert {m["name"] for m in res_a["missing"]} == {"Croquetas"}

    # Org B — independent count, no leak from A
    assert res_b["total_dishes"] == 3
    assert res_b["missing_count"] == 2
    assert {m["name"] for m in res_b["missing"]} == {"Tortilla", "Pulpo"}

    # Critical: org A's missing list contains nothing from org B and vice versa.
    a_names = {m["name"] for m in res_a["missing"]}
    b_names = {m["name"] for m in res_b["missing"]}
    assert a_names.isdisjoint(b_names)
    assert "Tortilla" not in a_names
    assert "Croquetas" not in b_names
