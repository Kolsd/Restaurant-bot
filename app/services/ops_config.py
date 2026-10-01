"""
app/services/ops_config.py
==========================
Which operational screens a sede uses (migration 0105).

The owner answers once, the first time they open Operación (`/staff`):
  - bar:      does the sede have a bar with its own screen — and if so, which
              carta categories are prepared there (Bebidas, Cócteles...);
  - delivery: does it take delivery/pickup orders (Domicilios — the cashier's
              queue of `/pedir` orders);
  - courier:  does it have its own couriers (Mis entregas — the courier's
              phone screen);
  - waiter:   do its waiters use the app (Mesero).
Caja and Cocina are always on.

A sede that never answered (`{}`) keeps the old behaviour: every screen, no
bar split. Each sede is its own restaurant, so this lives on the location,
never on the org.
"""
from __future__ import annotations

from typing import Optional

from app.repositories import ops_config_repo

OPTIONAL_SECTIONS: tuple[str, ...] = ("delivery", "waiter", "bar", "courier")
MAX_BAR_CATEGORIES = 50


def normalize(raw: Optional[dict]) -> dict:
    """Stored JSON → the full shape, with safe types and defaults."""
    raw = raw if isinstance(raw, dict) else {}
    configured = bool(raw.get("configured"))
    cats = raw.get("bar_categories")
    cats = [str(c).strip() for c in cats if str(c).strip()] if isinstance(cats, list) else []
    out = {"configured": configured, "bar_categories": cats[:MAX_BAR_CATEGORIES]}
    for key in OPTIONAL_SECTIONS:
        # Before the owner answers, everything shows (the pre-0105 behaviour).
        out[key] = bool(raw.get(key)) if configured else True
    if not out["bar"]:
        out["bar_categories"] = []
    return out


def visible_sections(sections: list[str], cfg: dict) -> list[str]:
    """Drop the optional screens this sede said it doesn't use."""
    if not cfg.get("configured"):
        return list(sections)
    return [s for s in sections if s not in OPTIONAL_SECTIONS or cfg.get(s)]


def station_features(features: Optional[dict], cfg: dict) -> dict:
    """The `features` the kitchen/bar split reads, with this sede's answer
    taking over once it has one. An unconfigured sede keeps whatever the org
    features said (legacy)."""
    features = dict(features or {})
    if cfg.get("configured"):
        features["bar_enabled"] = bool(cfg.get("bar")) and bool(cfg.get("bar_categories"))
        features["bar_categories"] = list(cfg.get("bar_categories") or [])
    return features


async def get_for_sede(org_id: int, location_id: Optional[int]) -> dict:
    """# Requires active tenant_scope(org_id)."""
    if not location_id:
        return normalize({})
    raw = await ops_config_repo.db_get_ops_config(org_id, int(location_id))
    return normalize(raw)
