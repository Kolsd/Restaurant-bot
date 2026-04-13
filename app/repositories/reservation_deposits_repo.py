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
async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()


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
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reservation_deposits
                (reservation_id, amount, currency, status, payment_url)
            VALUES ($1, $2, $3, 'pending', $4)
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


async def db_get_deposit_by_reservation(reservation_id: int) -> dict | None:
    """Return the most recent deposit for a reservation (pending or paid)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM reservation_deposits
            WHERE reservation_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            reservation_id,
        )
        return _serialize(dict(row)) if row else None


async def db_confirm_deposit(reservation_id: int, transaction_id: str) -> dict | None:
    """
    Transition deposit status pending -> paid.
    Returns the updated row, or None if no pending deposit was found.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE reservation_deposits
            SET status = 'paid',
                paid_at = NOW(),
                transaction_id = $1
            WHERE reservation_id = $2
              AND status = 'pending'
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


async def db_get_pending_deposits(older_than_hours: int = 2) -> list[dict]:
    """
    Return pending deposits older than `older_than_hours` for auto-cancellation.
    Used by the scheduler to expire unpaid deposits.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
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


async def db_mark_deposit_refunded(deposit_id: int, reason: str = "") -> dict | None:
    """Mark a deposit as refunded with an optional reason string."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE reservation_deposits
            SET refunded = TRUE,
                refund_reason = $1
            WHERE id = $2
            RETURNING *
            """,
            reason,
            deposit_id,
        )
        if row:
            log.info("deposit.refunded", deposit_id=deposit_id, reason=reason)
            return _serialize(dict(row))
        return None
