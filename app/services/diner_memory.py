"""
app/services/diner_memory.py
============================
The restaurant remembers a diner who said yes (migration 0104).

A web diner is anonymous and gets a new `web:<uuid>` identity every visit.
With consent, their browser keeps a random secret (localStorage
`mesio_diner_memory_key`, see diner-session.js) and the server stores only its
hash as the profile key (`customer_profiles.phone = 'device:<sha256>'`). Every
diner session opened from that browser at ANY sede of the org points at the
profile, so "lo de siempre" follows the diner across sedes (PM 2026-10-01:
memory is per organization). What can be ordered still comes from the sede
the diner is sitting at.

Rules:
  - Nothing is stored without consent. A browser that never said yes is a
    stranger every time, exactly as before.
  - The phone typed at checkout never unlocks the history on another device
    (no verification channel exists); it is kept for the restaurant only.
  - History is computed from the linked sessions' orders, never copied.
  - The greeting and "repetir pedido" are deterministic (no LLM), so they
    work on every plan; the bot only gets the same facts as prompt context.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from app.repositories import customer_profiles_repo as profiles_repo
from app.repositories import diner_sessions_repo
from app.services import blocks
from app.services import database as db
from app.services import orders
from app.services import sede_menu
from app.services.logging import get_logger

log = get_logger(__name__)

# crypto.randomUUID() is 36 chars; anything much shorter was not made by our
# page and is too guessable to stand for a person's history.
MIN_KEY_LEN = 32
MAX_KEY_LEN = 200
_KEY_PREFIX = "device:"

MAX_FAVORITES = 3


def device_key(raw: Optional[str]) -> Optional[str]:
    """The profile key for a browser secret, or None when it is unusable."""
    raw = (raw or "").strip()
    if len(raw) < MIN_KEY_LEN or len(raw) > MAX_KEY_LEN:
        return None
    return _KEY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _items(raw) -> list:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def _qty(item: dict) -> int:
    try:
        return max(1, int(item.get("quantity") or item.get("qty") or 1))
    except (TypeError, ValueError):
        return 1


def aggregate_history(rows: list[dict]) -> dict:
    """Pure: order rows (newest first, as the repo returns them) → summary.

    - visits: distinct diner sessions that ordered something.
    - last_visit: everything ordered in the most recent such session (all
      its rounds), merged by (dish, note), in the order it was asked for.
    - favorites: dishes ordered in at least two visits, most visits first,
      ties to the most recently ordered. A single visit has no favorites,
      only a last order.
    """
    visits: list[str] = []
    for r in rows:
        tok = r.get("session_token")
        if tok and tok not in visits:
            visits.append(tok)
    if not visits:
        return {"visits": 0, "last_visit": [], "favorites": []}

    last_token = visits[0]
    last: dict[tuple, dict] = {}
    for r in reversed([r for r in rows if r.get("session_token") == last_token]):
        for it in _items(r.get("items")):
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            note = str(it.get("note") or it.get("notes") or "").strip()
            key = (name.lower(), note.lower())
            if key in last:
                last[key]["qty"] += _qty(it)
            else:
                last[key] = {"name": name, "qty": _qty(it), "note": note}

    seen_in: dict[str, set] = {}
    display: dict[str, str] = {}
    recency: dict[str, int] = {}
    for idx, r in enumerate(rows):
        for it in _items(r.get("items")):
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            k = name.lower()
            seen_in.setdefault(k, set()).add(r.get("session_token"))
            display.setdefault(k, name)
            recency.setdefault(k, idx)
    ranked = sorted(
        (k for k, toks in seen_in.items() if len(toks) >= 2),
        key=lambda k: (-len(seen_in[k]), recency[k]),
    )
    favorites = [{"name": display[k], "count": len(seen_in[k])} for k in ranked[:MAX_FAVORITES]]
    return {"visits": len(visits), "last_visit": list(last.values()), "favorites": favorites}


async def summarize(org_id: int, profile_id: int) -> dict:
    """# Requires active tenant_scope(org_id)."""
    rows = await profiles_repo.get_profile_order_rows(org_id, profile_id)
    return aggregate_history(rows)


def _menu_index(menu: dict) -> dict[str, dict]:
    """lower(name) → dish (with its category) for the dishes this sede shows."""
    out: dict[str, dict] = {}
    for category, dishes in (menu or {}).items():
        if not isinstance(dishes, list):
            continue
        for d in dishes:
            if isinstance(d, dict) and d.get("name") and d.get("active", True) is not False:
                out.setdefault(str(d["name"]).strip().lower(), {**d, "category": category})
    return out


async def _orderable_here(org_id: int, location_id: Optional[int]) -> dict[str, dict]:
    """Dishes a diner can order at THIS sede right now, by lower(name)."""
    index = _menu_index(await sede_menu.get_sede_menu(org_id, location_id))
    if location_id is None:
        return index
    availability = await db.db_get_menu_availability(org_id, location_id)
    return {k: d for k, d in index.items() if availability.get(d["name"], True) is not False}


def _summary_line(lines: list[dict]) -> str:
    return ", ".join(
        (f"{l['qty']}× {l['name']}" if l["qty"] > 1 else l["name"]) for l in lines
    )


async def recognize_session(
    token: str, org_id: int, raw_key: Optional[str],
) -> Optional[dict]:
    """Link a freshly opened diner session to this browser's profile when the
    diner consented before. Returns the profile, or None for a stranger.

    # Requires active tenant_scope(org_id).
    """
    key = device_key(raw_key)
    if not key:
        return None
    profile = await profiles_repo.get_remembered_device(org_id, key)
    if not profile:
        return None
    await diner_sessions_repo.link_profile(token, org_id, profile["id"])
    log.info("diner_memory.recognized", org_id=org_id, profile_id=profile["id"])
    return profile


async def welcome_back_turn(
    org_id: int, location_id: Optional[int], profile: dict, currency: str,
) -> Optional[dict]:
    """The returning diner's greeting: their last order, a repeat button,
    and their favorites as dish cards — only what THIS sede can serve today.
    None when there is nothing to recall yet (consented, never ordered).

    # Requires active tenant_scope(org_id).
    """
    summary = await summarize(org_id, profile["id"])
    if not summary["last_visit"]:
        return None

    here = await _orderable_here(org_id, location_id)
    repeatable = [l for l in summary["last_visit"] if l["name"].lower() in here]
    favorite_dishes = [here[f["name"].lower()] for f in summary["favorites"] if f["name"].lower() in here]

    name = (profile.get("display_name") or "").strip()
    hello = f"¡Hola de nuevo, {name}!" if name else "¡Hola de nuevo!"
    message = f"{hello} La última vez pediste {_summary_line(summary['last_visit'])}."

    out_blocks: list[dict] = [{
        "type": "memory_actions",
        "can_repeat": bool(repeatable),
        "repeat_label": "Repetir mi último pedido",
        # Said up front, not discovered after tapping: this sede doesn't serve
        # some of it today.
        "unavailable": [l["name"] for l in summary["last_visit"] if l["name"].lower() not in here],
    }]
    if favorite_dishes:
        out_blocks.append({"type": "text", "text": "Tus favoritos:"})
        out_blocks.append(blocks.build_dish_cards_block(favorite_dishes, currency=currency))
    return {"message": message, "blocks": out_blocks}


async def repeat_last_order(
    token: str, org_id: int, location_id: Optional[int], profile_id: int,
) -> dict:
    """Put the last visit's dishes (with their notes) back in this session's
    cart at today's prices. Skips what this sede doesn't serve today.

    Returns {"cart", "added": [names], "skipped": [names]}; "cart" is None
    when nothing could be added.

    # Requires active tenant_scope(org_id).
    """
    summary = await summarize(org_id, profile_id)
    here = await _orderable_here(org_id, location_id)
    added: list[str] = []
    skipped: list[str] = []
    cart = None
    for line in summary["last_visit"]:
        menu_dish = here.get(line["name"].lower())
        # Exact name first (the menu's own spelling), then the same server
        # resolution a dish-card tap goes through: price and stock are never
        # taken from the old order.
        dish = await orders.resolve_dish_for_cart(
            org_id, name=menu_dish["name"], location_id=location_id,
        ) if menu_dish else None
        if dish is None or str(dish.get("name", "")).lower() != line["name"].lower():
            skipped.append(line["name"])
            continue
        result = await orders.add_cart_line(token, org_id, dish, line["qty"], line["note"] or None)
        if not result.get("success"):
            skipped.append(line["name"])
            continue
        cart = result["cart"]
        added.append(dish["name"])
    return {"cart": cart, "added": added, "skipped": skipped}


async def prompt_context(token: str, org_id: int) -> tuple[str, list]:
    """What the bot may know about this diner: (customer_context, order_history)
    for agent.build_system_prompt. ("", []) for a diner who isn't remembered.

    # Requires active tenant_scope(org_id).
    """
    profile_id = await diner_sessions_repo.get_profile_id(token, org_id)
    if not profile_id:
        return "", []
    profile = await profiles_repo.get_profile_by_id(org_id, profile_id)
    if not profile:
        return "", []
    summary = await summarize(org_id, profile["id"])
    if summary["visits"]:
        profile = {
            **profile,
            "total_orders": summary["visits"],
            "last_order_summary": _summary_line(summary["last_visit"]),
        }
    return profiles_repo.serialize_for_prompt(profile), summary["favorites"]


async def profile_id_for_session(token: str, org_id: int) -> Optional[int]:
    """The remembered profile behind this diner session, or None.

    # Requires active tenant_scope(org_id).
    """
    return await diner_sessions_repo.get_profile_id(token, org_id)
