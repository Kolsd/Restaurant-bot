"""
app/services/blocks.py
=======================
The "blocks" protocol — structured UI hints returned alongside agent.chat()'s
plain-text `message`, consumed by the Mesio-native diner chat frontend
(app/routes/diner.py). Mesio is moving off WhatsApp onto its own chat surface;
`message` remains a readable text fallback (history, logs, LLM context) but no
longer has to carry the whole UX alone.

CONTRACT — this is a shape shared with a frontend agent. Do NOT rename fields
or invent new block types:

    {"type":"text","text":"..."}
    {"type":"dish_cards","dishes":[{"sku","name","description","price",
        "image_url","tags","badges","allergens","calories","prep_time_min"}]}
    {"type":"category_chips","chips":[{"label":"Pastas","value":"cat:Pastas"}]}
    {"type":"cart_summary","items":[{"line_id","sku","name","qty","unit_price",
        "subtotal","note"}],"subtotal":<number>,"currency":"COP"}
    {"type":"payment_options","options":[{"label","value"}]}
    {"type":"waiter_ack","reason":"bill|cutlery|napkins|other","text":"..."}

Money crossing the JSON boundary is ALWAYS float(quantize_money(...)) — never
raw float arithmetic (CLAUDE.md "Capa Financiera Decimal").

Dish fields are projected from normalize_dish_shape() in
app/repositories/restaurant_repo.py — this module does not invent a second
dish shape.

Block-hint side channel
------------------------
Some blocks (waiter_ack today) originate deep inside the tool-execution
pipeline (agent_salon.execute_salon_action, agent.execute_action) where
threading a new return value through every call site would touch code
governed by CLAUDE.md "Reglas del Bot — NO ROMPER" (execute_action /
execute_salon_action return a plain `str`, and many callers pattern-match on
that). Instead, call sites push a block onto a per-turn ContextVar via
push_block(); agent.chat() drains it once, when it builds the final response
dict for the turn. This is purely additive: it can never change what the bot
SAYS (the `message` string), only what extra structured hints ride alongside
it. push_block()/drain_blocks() never raise — a failure in this side channel
must never silence or break the actual reply (Rule 8, "Nunca silencio al
cliente").

This module intentionally has NO import-time dependency on agent.py,
agent_salon.py, or agent_external.py, so all three (plus routes/diner.py) can
import it without creating a cycle.
"""

from __future__ import annotations

import contextvars
from typing import Optional

from app.services.logging import get_logger
from app.services.money import ZERO, money_mul, money_sum, quantize_money, to_decimal

log = get_logger(__name__)

_VALID_WAITER_REASONS = ("bill", "cutlery", "napkins", "other")

# Per-turn bucket of block hints. None = no active turn (push_block is then a
# safe no-op — e.g. scheduler tasks, tests, or legacy call sites that predate
# the diner chat channel).
_active_blocks: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "mesio_active_blocks", default=None
)


def begin_turn() -> "contextvars.Token":
    """Start a fresh block-hint bucket for one agent.chat() turn.

    Call at the very top of the turn; pair with end_turn(token) in a
    `finally` so the bucket never leaks into an unrelated call.
    """
    return _active_blocks.set([])


def end_turn(token: "contextvars.Token") -> None:
    try:
        _active_blocks.reset(token)
    except Exception:
        pass


def push_block(block: dict) -> None:
    """Queue a block for the CURRENT turn. No-op outside begin_turn(); never
    raises — this is a best-effort side channel, not part of the reply path.
    """
    try:
        bucket = _active_blocks.get()
        if bucket is not None and isinstance(block, dict):
            bucket.append(block)
    except Exception:
        log.exception("blocks.push_failed")


def drain_blocks() -> list:
    """Return the blocks queued so far this turn (does not clear the bucket —
    end_turn()/begin_turn() own the lifecycle)."""
    try:
        bucket = _active_blocks.get()
        return list(bucket) if bucket else []
    except Exception:
        return []


# ── Pure builders (no side effects, no I/O) ───────────────────────────────

def build_text_block(text: str) -> dict:
    return {"type": "text", "text": text or ""}


def build_dish_cards_block(dishes: list, currency: str = "COP") -> dict:
    """`dishes` are raw menu dish dicts of whatever shape a restaurant's menu
    JSONB has accrued (old or new catálogo v2 fields) — normalize_dish_shape()
    is the single source of truth for defaults.
    """
    from app.repositories.restaurant_repo import normalize_dish_shape  # noqa: PLC0415

    out = []
    for raw in dishes or []:
        if not isinstance(raw, dict):
            continue
        d = normalize_dish_shape(raw)
        sku = raw.get("sku") or d.get("name") or ""
        price_dec = to_decimal(d.get("price", 0))
        out.append({
            "sku": sku,
            "name": d.get("name", ""),
            "description": d.get("description", ""),
            "price": float(quantize_money(price_dec, currency)),  # JSON boundary
            "image_url": d.get("image_url"),
            "tags": d.get("tags", []),
            "badges": d.get("badges", []),
            "allergens": d.get("allergens", []),
            "calories": d.get("calories"),
            "prep_time_min": d.get("prep_time_min"),
        })
    return {"type": "dish_cards", "dishes": out}


def build_category_chips_block(categories: list) -> dict:
    chips = [
        {"label": str(c), "value": f"cat:{c}"}
        for c in (categories or [])
        if c
    ]
    return {"type": "category_chips", "chips": chips}


def build_cart_summary_block(cart: dict, currency: str = "COP", allow_empty: bool = False) -> Optional[dict]:
    """Build from the `carts.cart_data` shape produced by orders.add_to_cart /
    orders.add_cart_line: {"items":[{"line_id","name","price","quantity",
    "subtotal","category","note","sku"}], ...}. `sku` and `line_id` may be
    absent on a legacy item — this is purely additive over the original
    shape (see CLAUDE.md "Part 1 — Cart mutations" / diner_sessions_repo).

    Returns None for an empty/missing cart UNLESS allow_empty=True, in which
    case an empty cart_summary block (items=[], subtotal=0) is returned —
    used by the diner cart tap-endpoints so the UI can render "cart is
    empty" state and clear the cart chip after removing the last item.
    """
    items = (cart or {}).get("items") or []
    if not items and not allow_empty:
        return None

    out_items = []
    running_total = ZERO
    for it in items:
        if not isinstance(it, dict):
            continue
        unit_price = to_decimal(it.get("price", 0))
        try:
            qty = int(it.get("quantity") or 1)
        except (ValueError, TypeError):
            qty = 1
        raw_subtotal = it.get("subtotal")
        line_subtotal = quantize_money(
            to_decimal(raw_subtotal) if raw_subtotal is not None else money_mul(unit_price, qty),
            currency,
        )
        running_total = money_sum([running_total, line_subtotal])
        out_items.append({
            "line_id": it.get("line_id") or "",
            "sku": it.get("sku") or it.get("name", ""),
            "name": it.get("name", ""),
            "qty": qty,
            "unit_price": float(quantize_money(unit_price, currency)),  # JSON boundary
            "subtotal": float(line_subtotal),  # JSON boundary
            "note": it.get("note") or "",
        })

    return {
        "type": "cart_summary",
        "items": out_items,
        "subtotal": float(quantize_money(running_total, currency)),  # JSON boundary
        "currency": currency,
    }


def build_payment_options_block(payment_methods: list) -> dict:
    options = [
        {"label": str(m), "value": str(m)}
        for m in (payment_methods or [])
        if m
    ]
    return {"type": "payment_options", "options": options}


def build_waiter_ack_block(reason: str, text: str) -> dict:
    if reason not in _VALID_WAITER_REASONS:
        reason = "other"
    return {"type": "waiter_ack", "reason": reason, "text": text or ""}
