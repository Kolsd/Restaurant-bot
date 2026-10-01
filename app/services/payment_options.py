"""
app/services/payment_options.py
===============================
How a diner can pay at THIS sede — the same list for the table and for /pedir.

The methods are the sede's own `payment_methods` (set per sede in Sedes →
domicilios, app/services/delivery.ALLOWED_PAYMENT_METHODS). A sede that never
chose any keeps the old table behaviour: tarjeta (datáfono) or efectivo.

Nequi / Bancolombia are transfers: the diner needs WHERE to send the money —
the restaurant's own text in Ajustes → "Instrucciones de pago"
(`organizations.features.payment_instructions`) — and then uploads the
receipt, which Caja checks in "Comprobantes". There is no gateway: nobody
types card data into Mesio (PM decisions 2026-09-11, kept 2026-10-01).
"""
from __future__ import annotations

import json
from typing import Optional

from app.repositories import delivery_repo
from app.services import database as db
from app.services import delivery as delivery_service

DEFAULT_TABLE_METHODS: tuple[str, ...] = ("tarjeta", "efectivo")
TRANSFER_METHODS: tuple[str, ...] = ("nequi", "bancolombia")
LABELS = {
    "efectivo": "Efectivo",
    "tarjeta": "Tarjeta (datáfono)",
    "nequi": "Nequi",
    "bancolombia": "Bancolombia",
}
# The table checkout's original keys, still sent by an older open page.
LEGACY_KEYS = {"card": "tarjeta", "cash": "efectivo"}


def kind_of(key: str) -> str:
    if key == "efectivo":
        return "cash"
    if key == "tarjeta":
        return "card"
    return "transfer"


def _features(raw) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


def transfer_instructions(features: Optional[dict]) -> dict[str, str]:
    """{method: the restaurant's own "send it here" text}, only non-empty ones.
    Keys may have been saved capitalised by the old settings page."""
    raw = _features(features).get("payment_instructions")
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in TRANSFER_METHODS:
        text = raw.get(key) or raw.get(key.capitalize()) or ""
        text = str(text).strip()
        if text:
            out[key] = text[:500]
    return out


async def sede_methods(org_id: int, location_id: Optional[int]) -> list[dict]:
    """[{key, label, kind, instructions}] in the sede's order.

    # Requires active tenant_scope(org_id).
    """
    org = await db.db_get_restaurant_by_org_id(org_id) or {}
    location = await db.db_get_location_by_id(location_id) if location_id else None
    if location and int(location.get("org_id") or -1) != org_id:
        location = None
    raw_cfg = await delivery_repo.db_get_location_delivery_config(org_id, int(location_id)) if location else {}
    cfg = delivery_service.get_delivery_config(org, {**(location or {}), "delivery_config": raw_cfg})
    keys = [k for k in cfg["payment_methods"] if k in delivery_service.ALLOWED_PAYMENT_METHODS]
    keys = list(dict.fromkeys(keys)) or list(DEFAULT_TABLE_METHODS)
    instructions = transfer_instructions(org.get("features"))
    return [
        {
            "key": k,
            "label": LABELS.get(k, k.capitalize()),
            "kind": kind_of(k),
            "instructions": instructions.get(k, ""),
        }
        for k in keys
    ]
