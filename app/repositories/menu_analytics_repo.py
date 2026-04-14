"""
app/repositories/menu_analytics_repo.py

Repository for catalog v2 menu event analytics (heatmap / menu engineering).
Follows Fase 6 Repository Pattern: lazy pool import, no SQL in routes/services.

Tables: menu_events
"""
from __future__ import annotations

from statistics import median
from typing import Optional

from app.services.logging import get_logger

log = get_logger(__name__)


async def _get_pool():
    from app.services.database import get_pool
    return await get_pool()


# ── Write ─────────────────────────────────────────────────────────────────────

async def record_event(
    restaurant_id: int,
    dish_name: str,
    event_type: str,
    phone: Optional[str] = None,
    bot_number: Optional[str] = None,
) -> None:
    """
    Insert a menu interaction event.
    Fire-and-forget: exceptions are logged but never re-raised to the caller.
    """
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO menu_events (restaurant_id, dish_name, event_type, phone, bot_number)
                VALUES ($1, $2, $3, $4, $5)
                """,
                restaurant_id,
                dish_name,
                event_type,
                phone or None,
                bot_number or None,
            )
    except Exception as exc:
        log.exception(
            "menu_analytics.record_event.error",
            restaurant_id=restaurant_id,
            dish_name=dish_name,
            event_type=event_type,
            error=str(exc),
        )


# ── Read ──────────────────────────────────────────────────────────────────────

async def get_dish_analytics(
    restaurant_id: int,
    days: int = 30,
) -> list[dict]:
    """
    Return per-dish event aggregates for the given period.

    Each entry: {dish_name, views, modal_opens, add_to_carts, orders,
                 cart_conversion_rate, order_conversion_rate}

    cart_conversion_rate  = add_to_carts / views      (or 0 if views == 0)
    order_conversion_rate = orders / add_to_carts     (or 0 if add_to_carts == 0)
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                dish_name,
                COUNT(*) FILTER (WHERE event_type = 'view')        AS views,
                COUNT(*) FILTER (WHERE event_type = 'modal_open')  AS modal_opens,
                COUNT(*) FILTER (WHERE event_type = 'add_to_cart') AS add_to_carts,
                COUNT(*) FILTER (WHERE event_type = 'ordered')     AS orders
            FROM menu_events
            WHERE restaurant_id = $1
              AND created_at >= NOW() - ($2 || ' days')::INTERVAL
            GROUP BY dish_name
            ORDER BY views DESC, dish_name
            """,
            restaurant_id,
            str(days),
        )

    result = []
    for row in rows:
        views        = int(row["views"])
        add_to_carts = int(row["add_to_carts"])
        orders       = int(row["orders"])

        cart_conv  = round(add_to_carts / views, 4)  if views        > 0 else 0.0
        order_conv = round(orders / add_to_carts, 4) if add_to_carts > 0 else 0.0

        result.append({
            "dish_name":            row["dish_name"],
            "views":                views,
            "modal_opens":          int(row["modal_opens"]),
            "add_to_carts":         add_to_carts,
            "orders":               orders,
            "cart_conversion_rate": cart_conv,
            "order_conversion_rate": order_conv,
        })

    return result


async def get_menu_engineering_matrix(
    restaurant_id: int,
    days: int = 30,
) -> dict:
    """
    Classify each dish into the 4 menu-engineering quadrants using the
    restaurant's own medians as thresholds (popularidad × conversión).

    Thresholds:
      - popularity threshold  = median(views)        across all dishes
      - conversion threshold  = median(orders/views) across dishes with views > 0

    Quadrants:
      stars      — high popularity  + high conversion  (keep, promote)
      puzzles    — low  popularity  + high conversion  (promote more)
      plowhorses — high popularity  + low  conversion  (redesign / reprice)
      dogs       — low  popularity  + low  conversion  (consider removing)

    Returns:
      {stars: [...], puzzles: [...], plowhorses: [...], dogs: [...]}
      Each item: {dish_name, views, orders, conversion}
    """
    per_dish = await get_dish_analytics(restaurant_id, days)

    if not per_dish:
        return {"stars": [], "puzzles": [], "plowhorses": [], "dogs": []}

    all_views      = [d["views"] for d in per_dish]
    all_conversion = [d["orders"] / d["views"] for d in per_dish if d["views"] > 0]

    pop_threshold  = median(all_views)      if all_views      else 0
    conv_threshold = median(all_conversion) if all_conversion else 0

    stars      = []
    puzzles    = []
    plowhorses = []
    dogs       = []

    for d in per_dish:
        conversion   = round(d["orders"] / d["views"], 4) if d["views"] > 0 else 0.0
        high_popular = d["views"]  >= pop_threshold
        high_convert = conversion  >= conv_threshold

        entry = {
            "dish_name":  d["dish_name"],
            "views":      d["views"],
            "orders":     d["orders"],
            "conversion": conversion,
        }

        if high_popular and high_convert:
            stars.append(entry)
        elif not high_popular and high_convert:
            puzzles.append(entry)
        elif high_popular and not high_convert:
            plowhorses.append(entry)
        else:
            dogs.append(entry)

    return {
        "stars":      stars,
        "puzzles":    puzzles,
        "plowhorses": plowhorses,
        "dogs":       dogs,
    }
