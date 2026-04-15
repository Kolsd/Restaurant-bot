"""
Customer profiles repository — Feature 2, Phase A (customer memory foundation).

Stores per-phone customer context scoped to a restaurant:
  - preferences (dietary, allergies, dislikes, favorite_dish, notes)
  - order history summary (last order, total orders, total spent)
  - visit tracking (first_seen, last_seen)

This module is the ONLY place that reads/writes customer_profiles.
Do NOT import agent.py, orders.py, or any bot runtime file here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.services.logging import get_logger
from app.services.money import quantize_money
from app.services.tenant_db import tenant_connection

log = get_logger(__name__)

# ── Module-level constants ────────────────────────────────────────────────────

VALID_PREFERENCE_KEYS: frozenset[str] = frozenset(
    {"dietary", "allergies", "dislikes", "favorite_dish", "notes"}
)
MAX_VALUE_CHARS: int = 200
MAX_SUMMARY_CHARS: int = 300
DEFAULT_PROMPT_MAX_CHARS: int = 400


# ── Repository functions ──────────────────────────────────────────────────────

async def get_profile(restaurant_id: int, phone: str) -> Optional[dict]:
    """Return the profile row as a dict, or None if not found.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, restaurant_id, phone, display_name, preferences,
                   last_order_summary, total_orders, total_spent,
                   first_seen, last_seen
              FROM customer_profiles
             WHERE restaurant_id = $1
               AND phone = $2
            """,
            restaurant_id,
            phone,
        )
    if row is None:
        return None
    return dict(row)


async def upsert_profile_from_message(
    restaurant_id: int,
    phone: str,
    display_name: Optional[str] = None,
) -> dict:
    """Insert or update last_seen + optional display_name. Returns the profile dict.

    Uses INSERT ... ON CONFLICT (restaurant_id, phone) DO UPDATE. Idempotent.
    When display_name is None the existing value is preserved via COALESCE.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO customer_profiles
                (restaurant_id, phone, display_name, last_seen)
            VALUES
                ($1, $2, $3, NOW())
            ON CONFLICT (restaurant_id, phone) DO UPDATE
                SET last_seen    = NOW(),
                    display_name = COALESCE($3, customer_profiles.display_name)
            RETURNING id, restaurant_id, phone, display_name, preferences,
                      last_order_summary, total_orders, total_spent,
                      first_seen, last_seen
            """,
            restaurant_id,
            phone,
            display_name,
        )
    return dict(row)


async def update_preference(
    restaurant_id: int,
    phone: str,
    key: str,
    value: str,
) -> None:
    """Update a single preference key in the preferences JSONB column.

    Uses jsonb_set to merge — does NOT replace the whole object.
    key must be one of VALID_PREFERENCE_KEYS; raises ValueError otherwise.
    value is truncated to MAX_VALUE_CHARS (no exception raised on long input).
    If the profile does not yet exist, it is created first.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if key not in VALID_PREFERENCE_KEYS:
        raise ValueError(
            f"Invalid preference key '{key}'. "
            f"Must be one of: {sorted(VALID_PREFERENCE_KEYS)}"
        )

    safe_value: str = value[:MAX_VALUE_CHARS]

    # Ensure the profile exists before we try to update it.
    profile = await get_profile(restaurant_id, phone)
    if profile is None:
        log.info(
            "customer_profiles.update_preference.creating_profile",
            restaurant_id=restaurant_id,
            phone=phone,
            key=key,
        )
        await upsert_profile_from_message(restaurant_id, phone, display_name=None)

    async with tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE customer_profiles
               SET preferences = jsonb_set(
                       preferences,
                       ARRAY[$3],
                       to_jsonb($4::text),
                       true           -- create_missing = true
                   )
             WHERE restaurant_id = $1
               AND phone = $2
            """,
            restaurant_id,
            phone,
            key,
            safe_value,
        )


async def increment_after_order(
    restaurant_id: int,
    phone: str,
    order_total: Decimal,
    order_summary: str,
) -> None:
    """Atomically increment total_orders, add to total_spent, set last_order_summary + last_seen.

    order_total MUST be a Decimal — floats are rejected (CLAUDE.md Rule 5).
    order_summary is truncated to MAX_SUMMARY_CHARS.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    if not isinstance(order_total, Decimal):
        raise TypeError(
            f"order_total must be Decimal, got {type(order_total).__name__}. "
            "Use Decimal() or to_decimal() from app.services.money."
        )

    safe_summary: str = order_summary[:MAX_SUMMARY_CHARS]
    quantized_total: Decimal = quantize_money(order_total)

    # Ensure the profile exists before the atomic update.
    profile = await get_profile(restaurant_id, phone)
    if profile is None:
        log.info(
            "customer_profiles.increment_after_order.creating_profile",
            restaurant_id=restaurant_id,
            phone=phone,
        )
        await upsert_profile_from_message(restaurant_id, phone, display_name=None)

    async with tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE customer_profiles
               SET total_orders       = total_orders + 1,
                   total_spent        = total_spent + $3,
                   last_order_summary = $4,
                   last_seen          = NOW()
             WHERE restaurant_id = $1
               AND phone = $2
            """,
            restaurant_id,
            phone,
            quantized_total,
            safe_summary,
        )


def serialize_for_prompt(
    profile: Optional[dict],
    max_chars: int = DEFAULT_PROMPT_MAX_CHARS,
) -> str:
    """Build a compact string for system prompt injection. Pure function (not async).

    Returns "" for None profiles or profiles with total_orders == 0 (new customer).
    Never includes phone, restaurant_id, or timestamps in the output.
    Newlines in stored values are replaced with spaces to preserve system prompt structure.
    Truncates to max_chars with ellipsis if needed.

    Example output:
      "Cliente: Miguel. 14 pedidos previos. Último: 2 hamburguesas + papas.
       Preferencias: vegetariano; sin maní; le gusta la coca cola light."
    """
    if profile is None:
        return ""

    total_orders: int = profile.get("total_orders") or 0
    if total_orders == 0:
        return ""

    parts: list[str] = []

    # Name
    display_name: Optional[str] = profile.get("display_name")
    if display_name:
        safe_name = display_name.replace("\n", " ").strip()
        parts.append(f"Cliente: {safe_name}.")

    # Order count
    parts.append(f"{total_orders} pedido{'s' if total_orders != 1 else ''} previo{'s' if total_orders != 1 else ''}.")

    # Last order summary
    last_summary: Optional[str] = profile.get("last_order_summary")
    if last_summary:
        safe_summary = last_summary.replace("\n", " ").strip()
        parts.append(f"Último: {safe_summary}.")

    # Preferences
    raw_prefs = profile.get("preferences")
    if raw_prefs:
        # asyncpg returns JSONB as a dict directly; handle both dict and str
        if isinstance(raw_prefs, str):
            import json as _json
            try:
                raw_prefs = _json.loads(raw_prefs)
            except Exception:
                raw_prefs = {}

        pref_fragments: list[str] = []
        for key in ("dietary", "allergies", "dislikes", "favorite_dish", "notes"):
            val = raw_prefs.get(key)
            if val:
                safe_val = str(val).replace("\n", " ").strip()
                pref_fragments.append(safe_val)

        if pref_fragments:
            parts.append("Preferencias: " + "; ".join(pref_fragments) + ".")

    result = " ".join(parts)

    if len(result) > max_chars:
        result = result[: max_chars - 1] + "…"

    return result
