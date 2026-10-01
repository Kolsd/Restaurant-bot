"""The carta a sede actually sells: the organization's menu plus that sede's
changes (PM 2026-09-20/21 — each sede is its own restaurant; the carta is the
org's, with per-sede overrides).

`apply_sede_changes` is pure so the merge rules live in one place and are
tested without a database. `get_sede_menu` is the ONE reader every surface
that shows or prices a dish for a sede must use — the bot, the diner chat,
`/pedir`, the POS, the cashier. Reading `organizations.menu` directly for a
sede would show a price the sede does not charge.

Merge rules:
  - a base dish the sede hid is dropped;
  - a base dish with a sede price takes that price (it survives later base
    price changes until someone removes it);
  - a sede's own dish is added to its category, created if missing. If the
    base later gains a dish with the same name, the sede's own dish wins —
    two dishes answering to one name would make `find_dish` ambiguous.

Dishes are matched by name, case-insensitively and trimmed, like
`menu_availability` and the per-sede inventory.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal, InvalidOperation

from app.repositories import sede_menu_repo
from app.repositories.restaurant_repo import normalize_dish_shape


def dish_key(name: str | None) -> str:
    return (name or "").strip().lower()


def _menu_number(price) -> int | float:
    """A sede price in the shape base prices have inside the menu JSON. The
    merged carta flows into carts that `state_store` serializes, so it must
    never carry a Decimal (bot rule 1)."""
    value = Decimal(str(price))
    return int(value) if value == value.to_integral_value() else float(value)  # JSON boundary


def apply_sede_changes(
    base_menu: dict | None,
    overrides: list[dict],
    own_dishes: list[dict],
) -> dict:
    """Return a NEW `{category: [dish, ...]}` menu; inputs are not mutated.

    `overrides`: rows with `dish_name`, `price` (Decimal | None), `hidden`.
    `own_dishes`: rows with `category` and `dish` (a dish dict).
    """
    by_key = {dish_key(o.get("dish_name")): o for o in overrides}
    own_keys = {dish_key((d.get("dish") or {}).get("name")) for d in own_dishes}

    result: dict = {}
    for category, dishes in (base_menu or {}).items():
        if not isinstance(dishes, list):
            continue
        kept: list = []
        for dish in dishes:
            if not isinstance(dish, dict):
                continue
            key = dish_key(dish.get("name"))
            if key in own_keys:
                continue
            ov = by_key.get(key)
            if ov and ov.get("hidden"):
                continue
            dish = copy.deepcopy(dish)
            if ov and ov.get("price") is not None:
                dish["price"] = _menu_number(ov["price"])
            kept.append(dish)
        result[category] = kept

    for row in own_dishes:
        raw = row.get("dish")
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            continue
        category = (row.get("category") or "").strip() or "Otros"
        result.setdefault(category, []).append(normalize_dish_shape(copy.deepcopy(raw)))

    return {c: d for c, d in result.items() if d}


async def get_sede_menu(org_id: int, location_id: int | None) -> dict:
    """The carta of one sede, already merged.

    `location_id` None means the caller has no sede to name: the result is
    the base menu. Only brand-level surfaces (`/r/{slug}`) may rely on that;
    everything that takes an order has a sede.

    # Requires active tenant_scope(org_id) or bypass_tenant_scope().
    """
    base = await sede_menu_repo.db_get_org_menu(org_id)
    if not location_id:
        return base
    overrides = await sede_menu_repo.db_list_overrides(org_id, location_id)
    own = await sede_menu_repo.db_list_own_dishes(org_id, location_id)
    return apply_sede_changes(base, overrides, own)


def parse_price(value) -> Decimal:
    """A menu price from a JSON body. Raises ValueError on anything that is
    not a non-negative number."""
    if isinstance(value, bool):
        raise ValueError("price")
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError("price") from exc
    if not price.is_finite() or price < 0:
        raise ValueError("price")
    return price.quantize(Decimal("0.01"))

