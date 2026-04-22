"""
Reservation deposits repository — Wompi prepayment guarantees.

Covers the reservation_deposits aggregate:
  - Create deposit record with payment URL
  - Query latest deposit by reservation
  - Confirm deposit after Wompi webhook approval
  - Fetch pending/stale deposits for expiry scheduler
  - Mark deposit as refunded
"""

from __future__ import annotations

from decimal import Decimal

from app.services.logging import get_logger

log = get_logger(__name__)


# Lazy accessors — break circular import with app.services.database.
def _tenant_connection():
    from app.services.tenant_db import tenant_connection  # noqa: PLC0415
    return tenant_connection()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


async def db_create_deposit(
    reservation_id: int,
    amount: Decimal | int | str,
    currency: str = "COP",
    payment_url: str = "",
) -> dict | None:
    """Insert a new deposit record and return the full row."""
    async with _tenant_connection() as conn:
        # location_id is derived from the restaurant_table assigned to the reservation.
        # app.location_id GUC is not set (only app.org_id is set by tenant_db.py),
        # so we JOIN via reservations.table_id → restaurant_tables.branch_id.
        # If no table has been assigned yet, falls back to the first active location
        # for the org (ORDER BY id ASC LIMIT 1) — always deterministic.
        row = await conn.fetchrow(
            """
            INSERT INTO reservation_deposits
                (reservation_id, amount, currency, status, payment_url,
                 org_id, location_id)
            VALUES ($1, $2, $3, 'pending', $4,
                    NULLIF(current_setting('app.org_id', true), '')::bigint,
                    COALESCE(
                        (SELECT rt.branch_id
                           FROM reservations r
                           JOIN restaurant_tables rt ON rt.id = r.table_id
                          WHERE r.id = $1
                          LIMIT 1),
                        (SELECT id FROM locations
                          WHERE org_id = NULLIF(current_setting('app.org_id', true), '')::bigint
                            AND active = TRUE
                          ORDER BY id ASC LIMIT 1)
                    ))
            RETURNING *
            """,
            reservation_id,
            amount,
            currency,
            payment_url,
        )
        if row:
            log.info(
                "deposit.created",
                reservation_id=reservation_id,
                amount=str(amount),
                currency=currency,
            )
            return _serialize(dict(row))
        return None


async def db_confirm_deposit(reservation_id: int, transaction_id: str) -> dict | None:
    """
    Transition deposit status pending -> paid.

    Uses a subquery to deterministically pick the OLDEST pending deposit
    when duplicate pending rows exist (shouldn't happen once the partial
    UNIQUE INDEX from migration 0032 is in place, but this is defensive).

    Returns the updated row, or None if no pending deposit was found.
    """
    async with _tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE reservation_deposits
            SET status = 'paid',
                paid_at = NOW(),
                transaction_id = $1
            WHERE id = (
                SELECT id FROM reservation_deposits
                 WHERE reservation_id = $2
                   AND status = 'pending'
                 ORDER BY created_at ASC
                 LIMIT 1
            )
            RETURNING *
            """,
            transaction_id,
            reservation_id,
        )
        if row:
            log.info(
                "deposit.confirmed",
                reservation_id=reservation_id,
                transaction_id=transaction_id,
            )
            return _serialize(dict(row))
        return None


async def db_confirm_deposit_and_reservation(
    reservation_id: int, transaction_id: str
) -> dict | None:
    """
    Atomically confirm the deposit AND transition the reservation to 'confirmed'
    in a single transaction.

    Picks the oldest pending deposit for the reservation (deterministic when
    duplicate pending rows exist).  If no pending deposit is found, returns None
    without touching the reservation row.

    Returns a dict with keys 'deposit' and 'reservation', or None on no-op.
    """
    async with _tenant_connection() as conn:
        async with conn.transaction():
            deposit_row = await conn.fetchrow(
                """
                UPDATE reservation_deposits
                SET status = 'paid',
                    paid_at = NOW(),
                    transaction_id = $1
                WHERE id = (
                    SELECT id FROM reservation_deposits
                     WHERE reservation_id = $2
                       AND status = 'pending'
                     ORDER BY created_at ASC
                     LIMIT 1
                )
                RETURNING *
                """,
                transaction_id,
                reservation_id,
            )
            if deposit_row is None:
                return None

            reservation_row = await conn.fetchrow(
                """
                UPDATE reservations
                   SET status = 'confirmed', confirmed_at = NOW(), deposit_paid = TRUE
                 WHERE id = $1
                RETURNING *
                """,
                reservation_id,
            )

    log.info(
        "deposit.confirmed_and_reservation_activated",
        reservation_id=reservation_id,
        transaction_id=transaction_id,
    )
    return {
        "deposit": _serialize(dict(deposit_row)),
        "reservation": _serialize(dict(reservation_row)) if reservation_row else None,
    }


async def db_get_pending_deposits(older_than_hours: int = 2) -> list[dict]:
    """
    Return pending deposits older than `older_than_hours` for auto-cancellation.
    Used by the scheduler to expire unpaid deposits.
    """
    async with _tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM reservation_deposits
            WHERE status = 'pending'
              AND created_at < NOW() - make_interval(hours => $1)
            ORDER BY id ASC
            """,
            older_than_hours,
        )
        return [_serialize(dict(r)) for r in rows]
