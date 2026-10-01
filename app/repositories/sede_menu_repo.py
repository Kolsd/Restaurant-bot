"""SQL for the per-sede carta (migration 0093): the org's base menu, each
sede's overrides of it, and the dishes only one sede sells.

The merge itself lives in `app/services/sede_menu.py`; nothing here decides
what a sede's carta looks like.

Every function requires an active tenant_scope(org_id) or
bypass_tenant_scope().
"""
from __future__ import annotations

import json
from decimal import Decimal

from app.repositories.restaurant_repo import (
    _normalize_menu_dishes,
    normalize_dish_shape,
    validate_dish_image_ownership,
)
from app.services.logging import get_logger

log = get_logger(__name__)


class SedeMenuError(ValueError):
    """A change to a sede's carta that must be refused, with a message the
    staff member can read."""


def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _json_default(o):
    # JSON boundary: Decimal prices go into the JSONB as numbers.
    return float(o) if isinstance(o, Decimal) else str(o)


def _decode_menu(raw) -> dict:
    """organizations.menu as a dict. Older rows hold a JSON string, some
    of them encoded twice."""
    value = raw
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            log.warning("sede_menu.base_menu_undecodable")
            return {}
    return _normalize_menu_dishes(value) if isinstance(value, dict) else {}


async def _assert_sede_of_org(conn, org_id: int, location_id: int) -> None:
    if not await conn.fetchval(
        "SELECT 1 FROM locations WHERE id = $1 AND org_id = $2", location_id, org_id,
    ):
        raise SedeMenuError("Esa sede no pertenece a tu organización")


async def db_get_org_menu(org_id: int) -> dict:
    async with _tenant_connection() as conn:
        raw = await conn.fetchval("SELECT menu FROM organizations WHERE id = $1", org_id)
    return _decode_menu(raw)


# ── Overrides of base dishes ─────────────────────────────────────────────────

async def db_list_overrides(org_id: int, location_id: int) -> list[dict]:
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT dish_name, price, hidden, updated_at
                 FROM location_menu_overrides
                WHERE org_id = $1 AND location_id = $2
                ORDER BY lower(dish_name)""",
            org_id, location_id,
        )
    return [dict(r) for r in rows]


async def db_set_override(
    org_id: int,
    location_id: int,
    dish_name: str,
    *,
    price: Decimal | None,
    hidden: bool,
) -> dict | None:
    """Set what this sede changes about one base dish. `price` None means
    "the base price". Changing nothing (no price, not hidden) removes the
    row, so an override never lingers as a no-op. Returns the stored row,
    or None when it was removed."""
    name = (dish_name or "").strip()
    if not name:
        raise SedeMenuError("Falta el nombre del plato")
    async with _tenant_connection() as conn:
        await _assert_sede_of_org(conn, org_id, location_id)
        if price is None and not hidden:
            await conn.execute(
                """DELETE FROM location_menu_overrides
                    WHERE org_id = $1 AND location_id = $2
                      AND lower(dish_name) = lower($3)""",
                org_id, location_id, name,
            )
            return None
        row = await conn.fetchrow(
            """INSERT INTO location_menu_overrides
                   (org_id, location_id, dish_name, price, hidden, updated_at)
               VALUES ($1, $2, $3, $4, $5, NOW())
               ON CONFLICT (org_id, location_id, lower(dish_name)) DO UPDATE
                   SET price = EXCLUDED.price,
                       hidden = EXCLUDED.hidden,
                       updated_at = NOW()
               RETURNING dish_name, price, hidden, updated_at""",
            org_id, location_id, name, price, hidden,
        )
    return dict(row)


# ── Dishes only one sede sells ───────────────────────────────────────────────

async def db_list_own_dishes(org_id: int, location_id: int) -> list[dict]:
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT category, dish_name, dish, updated_at
                 FROM location_menu_dishes
                WHERE org_id = $1 AND location_id = $2
                ORDER BY category, lower(dish_name)""",
            org_id, location_id,
        )
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d["dish"], str):
            d["dish"] = json.loads(d["dish"])
        out.append(d)
    return out


async def db_save_own_dish(
    org_id: int,
    location_id: int,
    category: str,
    dish: dict,
    *,
    previous_name: str | None = None,
) -> dict:
    """Create or replace a dish only this sede sells. `previous_name` renames
    an existing one. Refuses a name the base menu already has — the sede
    changes a base dish through an override, not by shadowing it."""
    category = (category or "").strip()
    if not category:
        raise SedeMenuError("Elige una categoría")
    if not isinstance(dish, dict) or not (dish.get("name") or "").strip():
        raise SedeMenuError("Falta el nombre del plato")
    if not validate_dish_image_ownership(dish, org_id):
        raise SedeMenuError("La imagen del plato no pertenece a este restaurante")
    shaped = normalize_dish_shape({**dish, "name": dish["name"].strip()})
    name = shaped["name"]
    old = (previous_name or name).strip()

    base = await db_get_org_menu(org_id)
    for dishes in base.values():
        for d in dishes if isinstance(dishes, list) else []:
            if isinstance(d, dict) and (d.get("name") or "").strip().lower() == name.lower():
                raise SedeMenuError(
                    f"'{name}' ya está en la carta general. Cambiale el precio "
                    "u ocultalo desde ahí en vez de crearlo de nuevo."
                )

    # The pool's jsonb codec encodes dicts itself; a pre-dumped string would
    # land double-encoded. Round-trip only to turn Decimal prices into numbers.
    payload = json.loads(json.dumps(shaped, default=_json_default))  # JSON boundary
    async with _tenant_connection() as conn:
        await _assert_sede_of_org(conn, org_id, location_id)
        async with conn.transaction():
            if old.lower() != name.lower():
                clash = await conn.fetchval(
                    """SELECT 1 FROM location_menu_dishes
                        WHERE org_id = $1 AND location_id = $2
                          AND lower(dish_name) = lower($3)""",
                    org_id, location_id, name,
                )
                if clash:
                    raise SedeMenuError(f"Ya existe un plato llamado '{name}' en esta sede")
                await conn.execute(
                    """DELETE FROM location_menu_dishes
                        WHERE org_id = $1 AND location_id = $2
                          AND lower(dish_name) = lower($3)""",
                    org_id, location_id, old,
                )
            row = await conn.fetchrow(
                """INSERT INTO location_menu_dishes
                       (org_id, location_id, category, dish_name, dish, updated_at)
                   VALUES ($1, $2, $3, $4, $5::jsonb, NOW())
                   ON CONFLICT (org_id, location_id, lower(dish_name)) DO UPDATE
                       SET category = EXCLUDED.category,
                           dish_name = EXCLUDED.dish_name,
                           dish = EXCLUDED.dish,
                           updated_at = NOW()
                   RETURNING category, dish_name, dish, updated_at""",
                org_id, location_id, category, name, payload,
            )
    out = dict(row)
    if isinstance(out["dish"], str):
        out["dish"] = json.loads(out["dish"])
    return out


async def db_delete_own_dish(org_id: int, location_id: int, dish_name: str) -> bool:
    async with _tenant_connection() as conn:
        result = await conn.execute(
            """DELETE FROM location_menu_dishes
                WHERE org_id = $1 AND location_id = $2
                  AND lower(dish_name) = lower($3)""",
            org_id, location_id, (dish_name or "").strip(),
        )
    return result != "DELETE 0"
