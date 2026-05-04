"""
Weekly owner reports repository — Feature 4, Round 1 (data layer only).

Computes last-week KPIs for a restaurant and persists the report record.
The scheduler tick (which calls these functions and sends the WhatsApp message)
is implemented in a later round — do NOT import scheduler or bot runtime here.

Query strategy (derived from existing repo patterns; post-Wave-2 the
canonical tenant key is org_id — restaurant_id mentions below describe
the legacy SHAPE preserved for backwards compat in function signatures,
not the column name):
- delivery/pickup orders  → JOIN restaurants r ON r.whatsapp_number = o.bot_number
                            WHERE r.id = $location_id  (restaurants is a VIEW
                            over locations JOIN organizations; r.id == locations.id)
- table orders            → WHERE org_id = $org_id
                            (post-Wave-2 + 0057: branch_id carries location_id, not org_id)
- NPS responses           → bot_number LIKE base_bot_number || '%'
                            where base_bot_number = split_part(r.whatsapp_number, '_b', 1)
                            This covers both exact matches and sucursal suffixes (_b<ts>).
- dormant customers       → customer_profiles WHERE org_id = $1
                            AND total_orders >= MIN_DORMANT_ORDERS
                            AND last_seen < NOW() - MAKE_INTERVAL(days => DORMANT_DAYS)
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Optional

from app.services.logging import get_logger
from app.services.money import to_decimal

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_DORMANT_ORDERS: int = 3      # customer must have >=3 orders to count as "frequent"
DORMANT_DAYS: int = 21           # not seen in >21 days

# Delivery/pickup statuses that count as revenue.
REVENUE_STATUSES_DELIVERY: tuple[str, ...] = ("entregado",)

# Table orders excluded from revenue (all others count once created).
REVENUE_STATUSES_TABLE_EXCLUDE: tuple[str, ...] = ("cancelado", "cancelled")


# ── Lazy pool / serialize accessors ──────────────────────────────────────────

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _s  # noqa: PLC0415
    return _s(d)


# ── Core stat computation ─────────────────────────────────────────────────────

async def compute_weekly_stats(
    restaurant_id: int,
    week_start: date,
    week_end: date,
) -> dict:
    """Return a KPI dict for the given restaurant and ISO week (week_start inclusive,
    week_end exclusive — i.e., week_end = week_start + 7 days).

    All monetary values are returned as Decimal.  Callers must convert to float
    at the JSON boundary (see save_report).

    Returned keys:
        revenue_current        Decimal
        revenue_previous       Decimal
        revenue_delta_pct      float | None   (None when previous == 0)
        order_count_current    int
        order_count_previous   int
        delivery_count         int
        table_count            int
        nps_avg                float | None   (None when no responses this week)
        nps_count              int
        reviews_public_new     int
        dormant_frequent_count int
        has_signal             bool
    """
    # Previous week window (same length as current).
    from datetime import timedelta
    prev_start = week_start - timedelta(days=7)
    prev_end = week_start  # exclusive

    async with _tenant_connection() as conn:
        # ── 1. Delivery/pickup revenue (orders table via bot_number join) ──────
        # orders.status must be in REVENUE_STATUSES_DELIVERY.
        # join: restaurants r ON r.whatsapp_number = o.bot_number WHERE r.id = $restaurant_id
        # This matches the pattern in db_get_delivery_orders (orders_repo.py).
        delivery_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(CASE WHEN o.created_at >= $2 AND o.created_at < $3
                                  THEN o.total ELSE 0 END), 0) AS revenue_current,
                COALESCE(SUM(CASE WHEN o.created_at >= $4 AND o.created_at < $2
                                  THEN o.total ELSE 0 END), 0) AS revenue_previous,
                COUNT(CASE WHEN o.created_at >= $2 AND o.created_at < $3
                           THEN 1 END)                         AS count_current,
                COUNT(CASE WHEN o.created_at >= $4 AND o.created_at < $2
                           THEN 1 END)                         AS count_previous
            FROM orders o
            JOIN restaurants r ON r.whatsapp_number = o.bot_number
            JOIN locations l ON l.id = r.id
            WHERE l.org_id = $1
              AND o.order_type IN ('domicilio', 'recoger')
              AND o.status = ANY($5)
            """,
            restaurant_id,
            week_start, week_end,    # $2, $3 — current window
            prev_start,              # $4 — prev window start (prev_end = week_start = $2)
            list(REVENUE_STATUSES_DELIVERY),  # $5
        )

        delivery_rev_current  = to_decimal(delivery_row["revenue_current"])
        delivery_rev_previous = to_decimal(delivery_row["revenue_previous"])
        delivery_count        = int(delivery_row["count_current"] or 0)
        delivery_count_prev   = int(delivery_row["count_previous"] or 0)

        # ── 2. Dine-in revenue (table_orders table) ──────────────────────────────
        # Post-Wave-2 + migration 0057: table_orders.branch_id carries location_id,
        # NOT org_id. Filtering branch_id = org_id matched zero rows for every
        # multi-sede org. Use org_id = $1 (the canonical tenant key) instead.
        # RLS already enforces org_id scoping; this is an explicit belt-and-suspenders.
        table_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(CASE WHEN o.created_at >= $2 AND o.created_at < $3
                                  THEN o.total ELSE 0 END), 0) AS revenue_current,
                COALESCE(SUM(CASE WHEN o.created_at >= $4 AND o.created_at < $2
                                  THEN o.total ELSE 0 END), 0) AS revenue_previous,
                COUNT(CASE WHEN o.created_at >= $2 AND o.created_at < $3
                           THEN 1 END)                         AS count_current,
                COUNT(CASE WHEN o.created_at >= $4 AND o.created_at < $2
                           THEN 1 END)                         AS count_previous
            FROM table_orders o
            WHERE o.status <> ALL($5)
              AND o.org_id = $1
            """,
            restaurant_id,
            week_start, week_end,
            prev_start,
            list(REVENUE_STATUSES_TABLE_EXCLUDE),  # $5
        )

        table_rev_current  = to_decimal(table_row["revenue_current"])
        table_rev_previous = to_decimal(table_row["revenue_previous"])
        table_count        = int(table_row["count_current"] or 0)
        table_count_prev   = int(table_row["count_previous"] or 0)

        # ── 3. Combined revenue + order counts ────────────────────────────────
        revenue_current  = delivery_rev_current  + table_rev_current
        revenue_previous = delivery_rev_previous + table_rev_previous
        order_count_current  = delivery_count + table_count
        order_count_previous = delivery_count_prev + table_count_prev

        if revenue_previous == Decimal("0"):
            revenue_delta_pct = None
        else:
            raw_pct = ((revenue_current - revenue_previous) / revenue_previous) * 100
            revenue_delta_pct = round(float(raw_pct), 1)

        # ── 4. NPS (nps_responses filtered by bot_number prefix) ──────────────
        # NPS rows store bot_number as-is at send time; sucursales have a suffix
        # like "_b1713000000".  Strategy: LIKE base_bot_number || '%'
        # where base_bot_number = split_part(r.whatsapp_number, '_b', 1).
        # This matches both the exact matriz number and any sucursal suffixes.
        nps_row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)          AS nps_count,
                AVG(nps_score)    AS nps_avg,
                COUNT(CASE WHEN is_public = TRUE THEN 1 END) AS reviews_public_new
            FROM nps_responses nr
            JOIN restaurants r ON r.id = $1
            WHERE nr.created_at >= $2
              AND nr.created_at <  $3
              AND nr.bot_number LIKE (split_part(r.whatsapp_number, '_b', 1) || '%')
            """,
            restaurant_id,
            week_start, week_end,
        )

        nps_count       = int(nps_row["nps_count"] or 0)
        nps_avg_raw     = nps_row["nps_avg"]
        nps_avg         = round(float(nps_avg_raw), 1) if nps_avg_raw is not None and nps_count > 0 else None
        reviews_public_new = int(nps_row["reviews_public_new"] or 0)

        # ── 5. Dormant frequent customers ─────────────────────────────────────
        dormant_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM customer_profiles
            WHERE org_id       = $1
              AND total_orders >= $2
              AND last_seen    <  NOW() - MAKE_INTERVAL(days => $3)
            """,
            restaurant_id,
            MIN_DORMANT_ORDERS,
            DORMANT_DAYS,
        )
        dormant_frequent_count = int(dormant_count or 0)

    # ── 6. has_signal ─────────────────────────────────────────────────────────
    has_signal = (
        revenue_current > Decimal("0")
        or nps_count > 0
        or delivery_count > 0
        or table_count > 0
    )

    return {
        "revenue_current":        revenue_current,
        "revenue_previous":       revenue_previous,
        "revenue_delta_pct":      revenue_delta_pct,
        "order_count_current":    order_count_current,
        "order_count_previous":   order_count_previous,
        "delivery_count":         delivery_count,
        "table_count":            table_count,
        "nps_avg":                nps_avg,
        "nps_count":              nps_count,
        "reviews_public_new":     reviews_public_new,
        "dormant_frequent_count": dormant_frequent_count,
        "has_signal":             has_signal,
    }


# ── Message formatting ────────────────────────────────────────────────────────

_MONTHS_ES = (
    "", "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def _fmt_cop(amount: float) -> str:
    """Format as COP zero-decimal with thousands dots: $3.200.000"""
    cents = int(round(amount))
    formatted = f"{cents:,}".replace(",", ".")
    return f"${formatted}"


def format_report_message(
    stats: dict,
    restaurant_name: str,
    week_start: date,
    week_end: date,
    dashboard_url: str,
) -> str:
    """Pure function — builds the Spanish WhatsApp message from stats.

    Returns "" when stats["has_signal"] is False (caller must NOT send).
    Handles edge cases:
    - revenue_previous == 0  → omit delta %, write "(primera semana con ventas)"
    - nps_count == 0         → omit NPS line entirely
    - dormant_freq == 0      → omit dormant customer line
    - no signal at all       → return ""
    """
    if not stats.get("has_signal", False):
        return ""

    # ── Dates ─────────────────────────────────────────────────────────────────
    # week_end is exclusive (day after last day), display the day before.
    from datetime import timedelta
    last_day = week_end - timedelta(days=1)
    month_name = _MONTHS_ES[week_start.month]
    start_str  = str(week_start.day)
    end_str    = str(last_day.day)

    # ── Revenue line ──────────────────────────────────────────────────────────
    rev_current  = stats["revenue_current"]
    rev_previous = stats["revenue_previous"]
    delta_pct    = stats["revenue_delta_pct"]

    rev_fmt = _fmt_cop(float(rev_current))  # JSON boundary — display only

    if delta_pct is None:
        # No previous revenue — first week with sales or brand-new restaurant.
        delta_line = " (primera semana con ventas 🚀)"
    elif delta_pct >= 0:
        delta_line = f" (+{delta_pct}% vs semana anterior)"
    else:
        delta_line = f" ({delta_pct}% vs semana anterior)"

    # ── Orders line ───────────────────────────────────────────────────────────
    total_orders  = stats["order_count_current"]
    delivery_cnt  = stats["delivery_count"]
    table_cnt     = stats["table_count"]

    # ── NPS block (optional) ──────────────────────────────────────────────────
    nps_count = stats["nps_count"]
    nps_avg   = stats["nps_avg"]

    if nps_count > 0 and nps_avg is not None:
        nps_block = f"⭐ NPS promedio: {nps_avg}/10 ({nps_count} respuesta{'s' if nps_count != 1 else ''})\n"
    else:
        nps_block = ""

    # ── Dormant customer block (optional) ─────────────────────────────────────
    dormant = stats["dormant_frequent_count"]
    if dormant > 0:
        customer_block = (
            f"👥 {dormant} cliente{'s' if dormant != 1 else ''} frecuente{'s' if dormant != 1 else ''} "
            f"sin visitar en +21 días\n"
        )
    else:
        customer_block = ""

    # ── Suggestion line ───────────────────────────────────────────────────────
    if dormant >= 3:
        suggestion_line = (
            f"💡 {dormant} clientes frecuentes no han vuelto en 3+ semanas. "
            "Un mensaje por WhatsApp podría traerlos de regreso."
        )
    elif delta_pct is not None and delta_pct >= 10:
        suggestion_line = f"💡 Creciste un {delta_pct}% — mantén ese ritmo. 💪"
    elif delta_pct is not None and delta_pct <= -10:
        abs_delta = abs(delta_pct)
        suggestion_line = (
            f"💡 Las ventas bajaron un {abs_delta}% esta semana. "
            "Una promo corta podría reactivar clientes."
        )
    else:
        suggestion_line = "💡 Semana estable. Revisa tus platos estrella y ajusta la carta si algo no se vende."

    # ── Assemble ──────────────────────────────────────────────────────────────
    message = (
        f"Hola 👋 Tu reporte Mesio — semana del {start_str} al {end_str} de {month_name}\n"
        f"\n"
        f"📈 Vendiste {rev_fmt}{delta_line}\n"
        f"🧾 {total_orders} pedido{'s' if total_orders != 1 else ''} "
        f"({delivery_cnt} domicilio · {table_cnt} mesa)\n"
        f"{nps_block}"
        f"{customer_block}"
        f"\n"
        f"{suggestion_line}\n"
        f"\n"
        f"Ver detalle → {dashboard_url}"
    )

    return message


# ── Persistence functions ─────────────────────────────────────────────────────

async def save_report(
    restaurant_id: int,
    week_start: date,
    payload: dict,
    message_text: str,
    owner_phone: Optional[str] = None,
    delivery_status: str = "pending",
    error_message: Optional[str] = None,
) -> Optional[dict]:
    """INSERT the report row.  ON CONFLICT (org_id, week_start) DO NOTHING.

    Returns the inserted row as a dict, or None if the row already existed
    (meaning the report was already generated for this week — idempotent).
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO weekly_reports
                (org_id, week_start, payload, message_text,
                 owner_phone, delivery_status, error_message)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7)
            ON CONFLICT (org_id, week_start) DO NOTHING
            RETURNING *
            """,
            restaurant_id,
            week_start,
            json.dumps(payload),   # asyncpg needs explicit json.dumps for ::jsonb cast
            message_text,
            owner_phone,
            delivery_status,
            error_message,
        )
    if row is None:
        log.info(
            "weekly_reports.save_report.conflict",
            restaurant_id=restaurant_id,
            week_start=week_start.isoformat(),
        )
        return None
    return _serialize(dict(row))


async def mark_sent(report_id: int) -> None:
    """Mark a report as successfully sent and bump the attempts counter."""
    async with _tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE weekly_reports
               SET sent_at         = NOW(),
                   delivery_status = 'sent',
                   attempts        = attempts + 1
             WHERE id = $1
            """,
            report_id,
        )


async def mark_failed(report_id: int, error: str) -> None:
    """Mark a report as failed, store the error, and bump attempts.

    Note: callers should consult MAX_SEND_ATTEMPTS to decide whether to
    keep the row in 'failed' for retry or transition to 'failed_permanent'.
    """
    async with _tenant_connection() as conn:
        await conn.execute(
            """
            UPDATE weekly_reports
               SET delivery_status = 'failed',
                   error_message   = $2,
                   attempts        = attempts + 1
             WHERE id = $1
            """,
            report_id,
            error,
        )


# Maximum number of WhatsApp send attempts per report row before we give up
# and transition the row to a terminal state. Exposed as a module constant so
# tests / scheduler share the same threshold.
MAX_SEND_ATTEMPTS: int = 3


async def get_retriable_report(restaurant_id: int, week_start: date) -> Optional[dict]:
    """Return the row for this (org, week) when it is retriable, else None.

    A report is retriable when:
      - delivery_status = 'failed'
      - attempts < MAX_SEND_ATTEMPTS

    Used by the scheduler to pick up rows that failed on a previous tick.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
              FROM weekly_reports
             WHERE org_id          = $1
               AND week_start      = $2
               AND delivery_status = 'failed'
               AND attempts        < $3
             LIMIT 1
            """,
            restaurant_id, week_start, MAX_SEND_ATTEMPTS,
        )
    return _serialize(dict(row)) if row else None


async def already_sent_for_week(restaurant_id: int, week_start: date) -> bool:
    """Return True if a sent report already exists for this restaurant + week."""
    async with _tenant_connection() as conn:
        row = await conn.fetchval(
            """
            SELECT 1 FROM weekly_reports
             WHERE org_id          = $1
               AND week_start      = $2
               AND delivery_status = 'sent'
             LIMIT 1
            """,
            restaurant_id,
            week_start,
        )
    return row is not None


async def get_recent_reports(restaurant_id: int, limit: int = 12) -> list[dict]:
    """Return the most-recent report rows for a restaurant (descending by week)."""
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT id, org_id, week_start, generated_at, sent_at,
                   payload, message_text, owner_phone, delivery_status, error_message
              FROM weekly_reports
             WHERE org_id = $1
             ORDER BY week_start DESC
             LIMIT $2
            """,
            restaurant_id,
            limit,
        )
    return [_serialize(dict(r)) for r in rows]


# ── JSONB payload builder (helper for callers) ────────────────────────────────

def build_payload(stats: dict, week_start: date, week_end: date) -> dict:
    """Convert the compute_weekly_stats dict to a JSON-safe payload dict.

    Decimal values are converted to float at this boundary only.
    """
    return {
        # JSON boundary — Decimal → float for JSONB storage
        **{
            k: (float(v) if isinstance(v, Decimal) else v)  # JSON boundary
            for k, v in stats.items()
        },
        "week_start": week_start.isoformat(),
        "week_end":   week_end.isoformat(),
    }
