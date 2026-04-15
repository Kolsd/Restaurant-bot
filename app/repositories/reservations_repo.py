"""
Reservations repository — Repository Pattern extraction.

Covers the reservations aggregate:
  - Upsert reservations (add/update by phone+bot+date+time)
  - Query by range, status, date, bot_number
  - Table availability check (exclude ±2h conflicts)
  - Status transitions: confirm, cancel, no_show
  - Stats aggregation
  - Confirmation-sent tracking for reminder workflows

Call sites that import via `app.services.database` should add a re-export shim
to that module once this repo is wired in.
"""

from __future__ import annotations

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── RESERVATIONS ──────────────────────────────────────────────────────────────

async def db_add_reservation(
    name: str,
    date_str: str,
    time_str: str,
    guests: int,
    phone: str,
    bot_number: str = "",
    notes: str = "",
) -> dict:
    """Upsert a reservation by phone + bot_number + date + time.

    If a matching row exists, UPDATE name/guests/notes and return it.
    Otherwise INSERT a new row.
    """
    async with _tenant_connection() as conn:
        existing = await conn.fetchrow(
            """SELECT * FROM reservations
               WHERE phone=$1 AND bot_number=$2 AND "date"=$3 AND "time"=$4""",
            phone, bot_number, date_str, time_str,
        )
        if existing:
            row = await conn.fetchrow(
                """UPDATE reservations
                   SET name=$1, guests=$2, notes=$3
                   WHERE id=$4
                   RETURNING *""",
                name, guests, notes, existing["id"],
            )
        else:
            row = await conn.fetchrow(
                """INSERT INTO reservations
                   (name, "date", "time", guests, phone, bot_number, notes, status, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', NOW())
                   RETURNING *""",
                name, date_str, time_str, guests, phone, bot_number, notes,
            )
        return _serialize(dict(row))


async def db_get_reservations_range(
    date_from: str,
    date_to: str,
    bot_number: str | None = None,
) -> list[dict]:
    """Return reservations between date_from and date_to (inclusive).

    Optionally filter by bot_number. Results ordered by date, time.
    """
    async with _tenant_connection() as conn:
        if bot_number is not None:
            rows = await conn.fetch(
                """SELECT * FROM reservations
                   WHERE "date" BETWEEN $1 AND $2 AND bot_number=$3
                   ORDER BY "date", "time" """,
                date_from, date_to, bot_number,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM reservations
                   WHERE "date" BETWEEN $1 AND $2
                   ORDER BY "date", "time" """,
                date_from, date_to,
            )
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_reservations(bot_number: str | None = None) -> list[dict]:
    """Return all reservations, optionally filtered by bot_number.

    Results ordered by created_at DESC.
    """
    async with _tenant_connection() as conn:
        if bot_number is not None:
            rows = await conn.fetch(
                "SELECT * FROM reservations WHERE bot_number=$1 ORDER BY created_at DESC",
                bot_number,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM reservations ORDER BY created_at DESC"
            )
        return [_serialize(dict(r)) for r in rows]


async def db_get_reservation_by_id(reservation_id: int) -> dict | None:
    """Return a single reservation by primary key, or None if not found."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM reservations WHERE id=$1",
            reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_update_reservation_status(
    reservation_id: int,
    status: str,
    **kwargs,
) -> dict | None:
    """Update status and any combination of allowed extra fields.

    Allowed kwargs: cancelled_at, cancellation_reason, confirmed_at, no_show.
    Returns the updated row or None if id not found.
    """
    allowed = {"cancelled_at", "cancellation_reason", "confirmed_at", "no_show"}
    sets = ["status=$1"]
    vals: list = [status]
    idx = 2
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k}=${idx}")
            vals.append(v)
            idx += 1
    vals.append(reservation_id)
    sql = f'UPDATE reservations SET {", ".join(sets)} WHERE id=${idx} RETURNING *'

    async with _tenant_connection() as conn:
        row = await conn.fetchrow(sql, *vals)
        return _serialize(dict(row)) if row else None


async def db_get_available_tables(
    date_str: str,
    time_str: str,
    guests: int,
    bot_number: str,
    branch_id: int | None = None,
) -> list[dict]:
    """Return restaurant tables with sufficient capacity that are not already
    booked within ±2 hours of the requested time on the same date.

    Resolves the restaurant via bot_number unless branch_id is provided.
    """
    async with _tenant_connection() as conn:
        # Resolve restaurant_id from bot_number or branch_id
        if branch_id is not None:
            restaurant_row = await conn.fetchrow(
                "SELECT id FROM restaurants WHERE id=$1",
                branch_id,
            )
        else:
            restaurant_row = await conn.fetchrow(
                "SELECT id FROM restaurants WHERE whatsapp_number=$1",
                bot_number,
            )

        if not restaurant_row:
            return []

        restaurant_id = restaurant_row["id"]

        rows = await conn.fetch(
            """SELECT t.* FROM restaurant_tables t
               WHERE t.restaurant_id=$1
                 AND t.active = TRUE
                 AND t.capacity >= $2
                 AND t.id NOT IN (
                     SELECT r.table_id FROM reservations r
                     WHERE r.table_id IS NOT NULL
                       AND r.status IN ('pending', 'confirmed')
                       AND r."date" = $3
                       AND ABS(EXTRACT(EPOCH FROM (
                           TO_TIMESTAMP(r."time", 'HH24:MI') - TO_TIMESTAMP($4, 'HH24:MI')
                       )) / 3600) < 2
                 )
               ORDER BY t.capacity""",
            restaurant_id, guests, date_str, time_str,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_assign_table_to_reservation(
    reservation_id: int,
    table_id: str,
) -> dict | None:
    """Assign a table to a reservation. Returns updated row or None."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE reservations SET table_id=$1 WHERE id=$2 RETURNING *",
            table_id, reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_mark_no_show(reservation_id: int) -> dict | None:
    """Mark a reservation as no-show. Returns updated row or None."""
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE reservations SET no_show=TRUE, status='no_show' WHERE id=$1 RETURNING *",
            reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_cancel_reservation(
    reservation_id: int,
    reason: str = "",
) -> dict | None:
    """Cancel a reservation, recording timestamp and optional reason.

    Returns updated row or None.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE reservations
               SET status='cancelled', cancelled_at=NOW(), cancellation_reason=$1
               WHERE id=$2
               RETURNING *""",
            reason, reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_get_reservations_by_status(
    bot_number: str,
    status: str,
    date_from: str | None = None,
    date_to: str | None = None,
    branch_id: int | None = None,
) -> list[dict]:
    """Return reservations filtered by bot_number and status.

    Optionally narrow by date range and/or branch_id. Ordered by date, time.
    """
    conditions = ["bot_number=$1", "status=$2"]
    vals: list = [bot_number, status]
    idx = 3

    if date_from is not None:
        conditions.append(f'"date" >= ${idx}')
        vals.append(date_from)
        idx += 1

    if date_to is not None:
        conditions.append(f'"date" <= ${idx}')
        vals.append(date_to)
        idx += 1

    if branch_id is not None:
        # Resolve restaurant_id for this branch and filter
        conditions.append(f"""id IN (
            SELECT res.id FROM reservations res
            JOIN restaurants r ON r.whatsapp_number = res.bot_number
            WHERE r.id = ${idx}
        )""")
        vals.append(branch_id)
        idx += 1

    where_clause = " AND ".join(conditions)
    sql = f'SELECT * FROM reservations WHERE {where_clause} ORDER BY "date", "time"'

    async with _tenant_connection() as conn:
        rows = await conn.fetch(sql, *vals)
        return [_serialize(dict(r)) for r in rows]


async def db_get_reservation_stats(
    bot_number: str,
    period_start: str,
    period_end: str,
    branch_id: int | None = None,
) -> dict:
    """Return aggregated reservation stats for a time period.

    Returns a dict with total, confirmed, cancelled, no_shows, avg_party_size,
    no_show_rate (computed in Python).
    """
    conditions = ['bot_number=$1 AND "date" >= $2 AND "date" <= $3']
    vals: list = [bot_number, period_start, period_end]
    idx = 4

    if branch_id is not None:
        conditions.append(f"""id IN (
            SELECT res.id FROM reservations res
            JOIN restaurants r ON r.whatsapp_number = res.bot_number
            WHERE r.id = ${idx}
        )""")
        vals.append(branch_id)
        idx += 1

    where_clause = " AND ".join(conditions)
    sql = f"""SELECT
        COUNT(*) AS total,
        COUNT(*) FILTER (WHERE status='confirmed') AS confirmed,
        COUNT(*) FILTER (WHERE status='cancelled') AS cancelled,
        COUNT(*) FILTER (WHERE no_show=TRUE) AS no_shows,
        COALESCE(AVG(guests), 0) AS avg_party_size
    FROM reservations
    WHERE {where_clause}"""

    async with _tenant_connection() as conn:
        row = await conn.fetchrow(sql, *vals)
        if not row:
            return {
                "total": 0,
                "confirmed": 0,
                "cancelled": 0,
                "no_shows": 0,
                "avg_party_size": 0.0,
                "no_show_rate": 0.0,
            }
        total = row["total"] or 0
        no_shows = row["no_shows"] or 0
        no_show_rate = round(no_shows / total, 4) if total > 0 else 0.0
        return {
            "total": total,
            "confirmed": row["confirmed"] or 0,
            "cancelled": row["cancelled"] or 0,
            "no_shows": no_shows,
            "avg_party_size": float(row["avg_party_size"] or 0),
            "no_show_rate": no_show_rate,
        }


async def db_confirm_reservation(reservation_id: int) -> dict | None:
    """Confirm a reservation, recording confirmed_at timestamp.

    Returns updated row or None.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE reservations
               SET status='confirmed', confirmed_at=NOW()
               WHERE id=$1
               RETURNING *""",
            reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_get_upcoming_unconfirmed(hours_ahead: int = 24) -> list[dict]:
    """Return confirmed reservations that have not yet received a reminder,
    whose appointment time falls within the next hours_ahead hours.
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM reservations
               WHERE status = 'confirmed'
                 AND confirmation_sent = FALSE
                 AND TO_TIMESTAMP("date" || ' ' || "time", 'YYYY-MM-DD HH24:MI') >= NOW()
                 AND TO_TIMESTAMP("date" || ' ' || "time", 'YYYY-MM-DD HH24:MI')
                     <= NOW() + make_interval(hours => $1)
               ORDER BY "date", "time" """,
            hours_ahead,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_mark_confirmation_sent(reservation_id: int) -> None:
    """Mark confirmation_sent=TRUE so the reminder is not sent again."""
    async with _tenant_connection() as conn:
        await conn.execute(
            "UPDATE reservations SET confirmation_sent=TRUE WHERE id=$1",
            reservation_id,
        )
