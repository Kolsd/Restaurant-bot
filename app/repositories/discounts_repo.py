"""
Discounts repository — dynamic time-slot discounts (yield management).

Covers:
  - Query currently active discount for a restaurant (right now)
  - List all discounts for config UI
  - Create / upsert a discount
  - Update allowed fields dynamically
  - Delete a discount
"""

from __future__ import annotations

import re
from zoneinfo import available_timezones

from app.services.logging import get_logger

log = get_logger(__name__)

_VALID_TIMEZONES = available_timezones()
_TZ_SAFE_RE = re.compile(r'^[A-Za-z0-9/_+-]+$')


def _validate_tz(tz: str) -> str:
    """Validate timezone is a known IANA name. Raises ValueError if not."""
    if tz not in _VALID_TIMEZONES or not _TZ_SAFE_RE.match(tz):
        raise ValueError(f"Invalid timezone: {tz}")
    return tz


# Lazy accessors — break circular import with app.services.database.
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# Mesio day_of_week convention: 0=Mon … 6=Sun
# Postgres EXTRACT(DOW …):       0=Sun … 6=Sat
# Conversion: mesio_dow = (pg_dow + 6) % 7

def _dow_expr(tz: str) -> str:
    """Return SQL expression for current Mesio day_of_week in the given timezone."""
    tz = _validate_tz(tz)
    return f"(EXTRACT(DOW FROM NOW() AT TIME ZONE '{tz}')::int + 6) % 7"

def _now_time_expr(tz: str) -> str:
    tz = _validate_tz(tz)
    return f"(NOW() AT TIME ZONE '{tz}')::time"


async def db_get_active_discount(
    restaurant_id: int,
    branch_id: int | None = None,
    tz: str = "America/Bogota",
) -> dict | None:
    """
    Return the best active discount for *this restaurant right now*, or None.

    Prefers branch-specific rows (branch_id IS NOT NULL) over restaurant-wide
    rows.  If branch_id is provided, the query checks both the branch row and
    the restaurant-wide row (branch_id IS NULL) and returns whichever gives
    the higher discount.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        dow = _dow_expr(tz)
        now_t = _now_time_expr(tz)
        if branch_id is not None:
            row = await conn.fetchrow(
                f"""
                SELECT *
                FROM   time_slot_discounts
                WHERE  restaurant_id   = $1
                  AND  active          = TRUE
                  AND  day_of_week     = {dow}
                  AND  start_time     <= {now_t}
                  AND  end_time       >  {now_t}
                  AND  (branch_id = $2 OR branch_id IS NULL)
                ORDER BY branch_id NULLS LAST, discount_percent DESC
                LIMIT 1
                """,
                restaurant_id,
                branch_id,
            )
        else:
            row = await conn.fetchrow(
                f"""
                SELECT *
                FROM   time_slot_discounts
                WHERE  restaurant_id   = $1
                  AND  active          = TRUE
                  AND  day_of_week     = {dow}
                  AND  start_time     <= {now_t}
                  AND  end_time       >  {now_t}
                  AND  branch_id IS NULL
                ORDER BY discount_percent DESC
                LIMIT 1
                """,
                restaurant_id,
            )

        return _serialize(dict(row)) if row else None


async def db_get_all_discounts(restaurant_id: int) -> list[dict]:
    """Return all discounts for a restaurant ordered for display."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT *
            FROM   time_slot_discounts
            WHERE  restaurant_id = $1
            ORDER  BY day_of_week, start_time
            """,
            restaurant_id,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_create_discount(
    restaurant_id: int,
    day_of_week: int,
    start_time: str,
    end_time: str,
    discount_percent: int,
    branch_id: int | None = None,
    label: str = "",
) -> dict:
    """
    Insert a new discount or update on conflict (same restaurant/branch/day/start_time).
    Returns the full row.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO time_slot_discounts
                (restaurant_id, branch_id, day_of_week, start_time, end_time,
                 discount_percent, label)
            VALUES ($1, $2, $3, $4::time, $5::time, $6, $7)
            ON CONFLICT (restaurant_id, branch_id, day_of_week, start_time)
            DO UPDATE SET
                end_time         = EXCLUDED.end_time,
                discount_percent = EXCLUDED.discount_percent,
                label            = EXCLUDED.label,
                active           = TRUE
            RETURNING *
            """,
            restaurant_id,
            branch_id,
            day_of_week,
            start_time,
            end_time,
            discount_percent,
            label,
        )
        return _serialize(dict(row))


_ALLOWED_UPDATE_FIELDS = {
    "day_of_week",
    "start_time",
    "end_time",
    "discount_percent",
    "label",
    "active",
    "branch_id",
}


async def db_update_discount(discount_id: int, **kwargs) -> dict | None:
    """
    Dynamically update allowed fields on a discount row.
    Returns the updated row or None if not found.
    """
    updates = {k: v for k, v in kwargs.items() if k in _ALLOWED_UPDATE_FIELDS}
    if not updates:
        log.warning("discounts_repo.update_discount.no_valid_fields", discount_id=discount_id)
        return None

    set_clauses = [f"{col} = ${i + 2}" for i, col in enumerate(updates)]
    values = list(updates.values())

    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE time_slot_discounts SET {', '.join(set_clauses)} WHERE id = $1 RETURNING *",
            discount_id,
            *values,
        )
        return _serialize(dict(row)) if row else None


async def db_delete_discount(discount_id: int) -> bool:
    """Delete a discount by id. Returns True if a row was deleted."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM time_slot_discounts WHERE id = $1",
            discount_id,
        )
        # asyncpg returns e.g. "DELETE 1"
        return result == "DELETE 1"
