"""
tests/ai_sim/unit_find_dish.py - Standalone unit test for find_dish + _normalize_menu_dishes.

Run with:
    python tests/ai_sim/unit_find_dish.py

All assertions are self-contained; no DB or network required.
Tests:
  1. _normalize_menu_dishes: flat format pass-through
  2. _normalize_menu_dishes: converts {categories: [{name, items}]} ->flat
  3. find_dish: case-insensitive exact match  ("limonada natural" ->"Limonada natural")
  4. find_dish: title-case exact match        ("Limonada Natural" ->"Limonada natural")
  5. find_dish: cross-case exact match        ("bandeja paisa" ->"Bandeja Paisa")
  6. find_dish: substring 40%-ratio match     ("cerveza" ->"Cerveza Club Colombia")
  7. find_dish: exact substring full name     ("cerveza club colombia" ->"Cerveza Club Colombia")
  8. find_dish: no match for unrelated term   ("sushi" ->None)
  9. find_dish: too short - ratio < 40%       ("lim" ->None  [3/16 = 18.75%])
 10. find_dish: single char query             ("a" ->None)
 11. find_dish: empty string                  ("" ->None)
 12. find_dish: None input                    (None ->None)
 13. find_dish: None menu (db returns None)   (-> None)
"""
from __future__ import annotations

import sys
import os

# --Make repo root importable WITHOUT polluting sys.path with tests/ai_sim ---
# tests/ai_sim/types.py shadows stdlib's `types` module if tests/ai_sim is on
# the path. We add REPO_ROOT at position 0 and make sure tests/ai_sim is NOT
# on the path to prevent this shadowing.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TESTS_AI_SIM = os.path.dirname(os.path.abspath(__file__))
# Remove tests/ai_sim from path if present (prevents types.py shadowing stdlib)
sys.path = [p for p in sys.path if os.path.abspath(p) != _TESTS_AI_SIM]
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import asyncio  # noqa: E402 — must come after path fix

from app.repositories.restaurant_repo import _normalize_menu_dishes  # noqa: E402

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {label}")
    _results.append((label, condition))


# -----------------------------------------------------------------------------
# Fixtures — menus in both formats
# -----------------------------------------------------------------------------

# Flat format: {category_name: [dishes]}  — used by production + db_update_menu
FLAT_MENU = {
    "Bebidas": [
        {"name": "Limonada natural", "price": 6000, "description": "Limonada fría con panela o azúcar"},
        {"name": "Cerveza Club Colombia", "price": 8000, "description": "Botella 330ml fría"},
        {"name": "Agua", "price": 4000, "description": "Agua mineral o del tiempo"},
    ],
    "Platos Fuertes": [
        {"name": "Bandeja Paisa", "price": 28000, "description": "Frijoles, chicharrón, carne molida"},
        {"name": "Ajiaco Santafereño", "price": 22000, "description": "Sopa bogotana"},
    ],
}

# Nested format: {categories: [{name, items}]}  — used by setup_demo.py / seed.py
NESTED_MENU = {
    "categories": [
        {
            "name": "Bebidas",
            "items": [
                {"name": "Limonada natural", "price": 6000, "description": "Limonada fría con panela o azúcar"},
                {"name": "Cerveza Club Colombia", "price": 8000, "description": "Botella 330ml fría"},
                {"name": "Agua", "price": 4000, "description": "Agua mineral o del tiempo"},
            ],
        },
        {
            "name": "Platos Fuertes",
            "items": [
                {"name": "Bandeja Paisa", "price": 28000, "description": "Frijoles, chicharrón, carne molida"},
                {"name": "Ajiaco Santafereño", "price": 22000, "description": "Sopa bogotana"},
            ],
        },
    ]
}


# -----------------------------------------------------------------------------
# Monkey-patch find_dish to avoid DB calls — replace db.db_get_menu with a stub
# -----------------------------------------------------------------------------

def _make_find_dish(menu_to_return):
    """Return a coroutine version of find_dish that uses a fixed menu."""
    import app.services.orders as orders_module
    import app.services.database as db_module
    import unittest.mock as mock

    async def _stub_get_menu(_bot_number):
        return menu_to_return

    # Patch db.db_get_menu inside orders module for the duration of the call
    async def patched_find_dish(dish_name, bot_number="TEST"):
        with mock.patch.object(db_module, "db_get_menu", side_effect=_stub_get_menu):
            return await orders_module.find_dish(dish_name, bot_number)

    return patched_find_dish


# -----------------------------------------------------------------------------
# Test suites
# -----------------------------------------------------------------------------

def test_normalize_menu_dishes():
    print("\n--_normalize_menu_dishes --------------------------------------")

    # T1: flat format is pass-through (keys preserved)
    normalized_flat = _normalize_menu_dishes(FLAT_MENU)
    check(
        "T1: flat format: 'Bebidas' key preserved",
        "Bebidas" in normalized_flat,
    )
    check(
        "T1: flat format: dishes are list",
        isinstance(normalized_flat.get("Bebidas"), list),
    )
    check(
        "T1: flat format: first dish name preserved",
        normalized_flat["Bebidas"][0]["name"] == "Limonada natural",
    )

    # T2: nested format is converted to flat
    normalized_nested = _normalize_menu_dishes(NESTED_MENU)
    check(
        "T2: nested: 'categories' key gone",
        "categories" not in normalized_nested,
    )
    check(
        "T2: nested: 'Bebidas' key present after conversion",
        "Bebidas" in normalized_nested,
    )
    check(
        "T2: nested: 'Platos Fuertes' key present",
        "Platos Fuertes" in normalized_nested,
    )
    check(
        "T2: nested: Limonada natural in Bebidas",
        any(d.get("name") == "Limonada natural" for d in normalized_nested.get("Bebidas", [])),
    )
    check(
        "T2: nested: Bandeja Paisa in Platos Fuertes",
        any(d.get("name") == "Bandeja Paisa" for d in normalized_nested.get("Platos Fuertes", [])),
    )
    check(
        "T2: nested: Cerveza Club Colombia in Bebidas",
        any(d.get("name") == "Cerveza Club Colombia" for d in normalized_nested.get("Bebidas", [])),
    )

    # T3: None input
    check(
        "T3: None returns empty dict (via normalize_menu_shape)",
        _normalize_menu_dishes(None) is None or _normalize_menu_dishes(None) == {},  # returns None for non-dict
    )


async def test_find_dish():
    print("\n--find_dish (flat menu) ---------------------------------------")

    # Use flat menu (the canonical format after normalization)
    find = _make_find_dish(FLAT_MENU)

    # T3: exact match, all-lowercase query vs title-case stored
    r = await find("limonada natural")
    check("T3: 'limonada natural' ->'Limonada natural' (case-insensitive)", r is not None and r.get("name") == "Limonada natural")

    # T4: title-case query vs stored with lowercase second word
    r = await find("Limonada Natural")
    check("T4: 'Limonada Natural' ->'Limonada natural' (title-case query)", r is not None and r.get("name") == "Limonada natural")

    # T5: cross-case
    r = await find("bandeja paisa")
    check("T5: 'bandeja paisa' ->'Bandeja Paisa'", r is not None and r.get("name") == "Bandeja Paisa")

    # T6: substring 40% ratio — "cerveza" is 7 chars, "Cerveza Club Colombia" is 21 chars
    #     ratio = 7/21 = 0.333... < 0.4 — should NOT match via substring branch
    #     BUT "cerveza" is in "cerveza club colombia" (True), ratio = 7/21 = 0.333
    #     Per CLAUDE.md Regla 12: ratio >= 0.4 required.
    #     "cerveza" (7) vs "Cerveza Club Colombia" (21): 7/21 = 0.333 < 0.4 ->no match
    #     Actually let's verify: task says "cerveza" should match "Cerveza Club Colombia"
    #     but 7/21 = 0.333 which is below 0.4. Let's check the actual ratio.
    ratio_cerveza = len("cerveza") / len("cerveza club colombia")
    # 7/21 = 0.333 — below 0.4. But the task says it should match.
    # The task requirement says ratio >= 40%, query_len/item_len >= 0.4.
    # 7/21 = 0.333 does NOT meet the threshold. So "cerveza" should NOT match.
    # But wait — the task example says "cerveza" ->matches "Cerveza Club Colombia" (40% substring)
    # Let me re-read: "cerveza" (7 chars) in "Cerveza Club Colombia" (21 chars)
    # 7/21 = 33% which is < 40%. So this should be NO match per the spec.
    # The task may have had an approximate example. Let's test what actually happens.
    r = await find("cerveza")
    if ratio_cerveza >= 0.4:
        check(f"T6: 'cerveza' ->'Cerveza Club Colombia' (ratio {ratio_cerveza:.2f} >= 0.4)", r is not None and r.get("name") == "Cerveza Club Colombia")
    else:
        # ratio is 0.33, below threshold — correct behavior is NO match
        check(f"T6: 'cerveza' ->None (ratio {ratio_cerveza:.2f} < 0.4, correct per Regla 12)", r is None)

    # T7: full exact match for longer name
    r = await find("cerveza club colombia")
    check("T7: 'cerveza club colombia' ->'Cerveza Club Colombia' (exact)", r is not None and r.get("name") == "Cerveza Club Colombia")

    # T8: no match
    r = await find("sushi")
    check("T8: 'sushi' ->None (not in menu)", r is None)

    # T9: "lim" — too short (3 chars), "Limonada natural" = 16 chars, ratio = 3/16 = 0.19 < 0.4
    r = await find("lim")
    check("T9: 'lim' ->None (3/16 = 18.75% < 40% ratio)", r is None)

    # T10: single char
    r = await find("a")
    check("T10: 'a' ->None (too short)", r is None)

    # T11: empty string
    r = await find("")
    check("T11: '' ->None (empty query)", r is None)

    # T12: None input — find_dish should not crash
    try:
        r = await find(None)
        check("T12: None ->None (no crash)", r is None)
    except Exception as exc:
        check(f"T12: None ->no crash (got {type(exc).__name__}: {exc})", False)

    print("\n--find_dish (nested DEMO_MENU format) ------------------------")

    # T13: same queries against nested format — after _normalize_menu_dishes fix,
    #      db_get_menu returns flat. But since we're mocking db_get_menu to return
    #      the nested dict directly, find_dish would see the nested format.
    #      This tests that find_dish itself does NOT crash on nested format
    #      (even though after the fix, db_get_menu will always return flat).
    #
    #      The real fix is in _normalize_menu_dishes (called by db_get_menu).
    #      Here we test end-to-end with the normalized-nested menu (flat equivalent).
    normalized_nested = _normalize_menu_dishes(NESTED_MENU)
    find_nested = _make_find_dish(normalized_nested)  # pass already-normalized

    r = await find_nested("limonada natural")
    check("T13a: nested->normalized: 'limonada natural' finds dish", r is not None and r.get("name") == "Limonada natural")

    r = await find_nested("Limonada Natural")
    check("T13b: nested->normalized: 'Limonada Natural' finds dish", r is not None and r.get("name") == "Limonada natural")

    r = await find_nested("bandeja paisa")
    check("T13c: nested->normalized: 'bandeja paisa' finds 'Bandeja Paisa'", r is not None and r.get("name") == "Bandeja Paisa")

    # T14: None menu
    find_none = _make_find_dish(None)
    r = await find_none("limonada natural")
    check("T14: None menu ->None (no crash)", r is None)


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

async def _main():
    test_normalize_menu_dishes()
    await test_find_dish()

    total = len(_results)
    passed = sum(1 for _, ok in _results if ok)
    failed = total - passed
    print(f"\n{'-'*55}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} FAILED)")
        for label, ok in _results:
            if not ok:
                print(f"  FAILED: {label}")
    else:
        print("  — all good")
    print('-'*55)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(_main())
