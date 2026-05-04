"""
Reservation deposit payment service — Wompi prepayment link generation
and webhook confirmation for the `reservation_deposits` feature flag.
"""

import os
import hashlib
from decimal import Decimal

from app.services.money import to_decimal, quantize_money
from app.services.orders import _wompi_credentials_from_restaurant
from app.repositories import reservation_deposits_repo as deposits_repo
from app.services.logging import get_logger

log = get_logger(__name__)

# NOTE: env-var Wompi credentials are LEGACY/FALLBACK. The canonical path is
# per-restaurant configuration via `features.wompi`. See `generate_deposit_link`.
WOMPI_PUBLIC_KEY       = os.getenv("WOMPI_PUBLIC_KEY", "")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "")
APP_DOMAIN             = os.getenv("APP_DOMAIN", "mesioai.com")

# Zero-decimal currencies (no cents multiplier needed)
_ZERO_DECIMAL_CURRENCIES = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}


async def generate_deposit_link(
    reservation_id: int,
    amount: Decimal,
    currency: str = "COP",
    restaurant: dict | None = None,
) -> str:
    """
    Generate a Wompi checkout link for a reservation deposit and persist the
    deposit record.  Returns the full payment URL string.

    Credential priority:
      1. `restaurant.features.wompi` (per-restaurant config) when `restaurant`
         is provided.
      2. Env vars `WOMPI_PUBLIC_KEY` / `WOMPI_INTEGRITY_SECRET` — legacy
         fallback for deploys not yet migrated.

    Raises RuntimeError if neither source supplies an integrity_secret — a link
    generated without it would have an invalid signature and Wompi would reject
    all payment attempts, creating silent data loss.
    """
    pk_override, integrity_override = _wompi_credentials_from_restaurant(restaurant)
    pk = pk_override or WOMPI_PUBLIC_KEY
    integrity = integrity_override or WOMPI_INTEGRITY_SECRET

    # Bug 9 fix: fail-fast if secret is missing rather than generating a broken link
    if not integrity:
        log.error("reservation_payments.integrity_secret_missing")
        raise RuntimeError("WOMPI_INTEGRITY_SECRET not configured")

    quantized = quantize_money(amount, currency)

    # Wompi expects amount-in-cents; for zero-decimal currencies the
    # "cents" value equals the face amount (already integer-like).
    if currency in _ZERO_DECIMAL_CURRENCIES:
        amount_cents = int(quantized)
    else:
        amount_cents = int(quantized * 100)

    reference = f"dep_{reservation_id}"

    # Wompi integrity signature: reference + amount_cents + currency + secret
    integrity_str = f"{reference}{amount_cents}{currency}{integrity}"
    signature = hashlib.sha256(integrity_str.encode()).hexdigest()

    redirect_url = f"https://{APP_DOMAIN}/api/reservations/{reservation_id}"

    payment_url = (
        f"https://checkout.wompi.co/p/?public-key={pk}"
        f"&currency={currency}"
        f"&amount-in-cents={amount_cents}"
        f"&reference={reference}"
        f"&redirect-url={redirect_url}"
        f"&signature%3Aintegrity={signature}"
    )

    await deposits_repo.db_create_deposit(
        reservation_id=reservation_id,
        amount=quantized,  # Decimal — asyncpg handles NUMERIC natively
        currency=currency,
        payment_url=payment_url,
    )

    log.info(
        "deposit.link_generated",
        reservation_id=reservation_id,
        amount=str(quantized),
        currency=currency,
    )
    return payment_url


async def confirm_deposit_payment(reference: str, transaction_id: str) -> bool:
    """
    Handle a Wompi `transaction.updated` APPROVED event for a deposit reference.

    Uses db_confirm_deposit_and_reservation() to atomically confirm the deposit
    row AND transition the reservation to confirmed status in a single DB
    transaction, eliminating the TOCTOU window in the previous two-step
    approach.

    Special-cases a reservation that is already 'cancelled': marks the deposit
    as paid (the client did pay) but does NOT confirm the reservation.  Logs a
    CRITICAL warning that requires manual reimbursement.

    Returns True if the deposit was found and processed (even if the reservation
    was already cancelled).  Returns False if no matching pending deposit exists.
    """
    if not reference.startswith("dep_"):
        return False

    try:
        reservation_id = int(reference.replace("dep_", ""))
    except ValueError:
        log.warning("deposit.bad_reference", reference=reference)
        return False

    # Bug 11 fix: check reservation status before confirming.
    # We fetch the reservation here (read-only, no repo boundary violation)
    # because reservations_repo is not in the permitted file list.
    from app.services import database as db  # noqa: PLC0415 — lazy, avoids cycle

    reservation = await db.db_get_reservation_by_id(reservation_id)

    if reservation and reservation.get("status") == "cancelled":
        # Bug 11: client paid for a cancelled reservation.
        # Mark only the deposit as paid; do NOT confirm the reservation.
        # Requires manual reimbursement — log CRITICAL.
        deposit_result = await deposits_repo.db_confirm_deposit(reservation_id, transaction_id)
        if deposit_result:
            log.error(
                "wompi.deposit.reservation_already_cancelled",
                reservation_id=reservation_id,
                transaction_id=transaction_id,
                action="deposit_marked_paid_manual_refund_required",
            )
        else:
            log.warning(
                "wompi.deposit.orphaned",
                reference=reference,
                transaction_id=transaction_id,
                reason="no_pending_deposit_and_reservation_cancelled",
            )
        # Return True: we handled the event; Wompi should not retry.
        return True

    # Atomic: confirm deposit + reservation in one transaction (Bug 4 fix)
    result = await deposits_repo.db_confirm_deposit_and_reservation(
        reservation_id, transaction_id
    )

    if result:
        log.info(
            "deposit.confirmed",
            reservation_id=reservation_id,
            transaction_id=transaction_id,
        )
        return True

    log.warning(
        "deposit.confirm_noop",
        reservation_id=reservation_id,
        transaction_id=transaction_id,
    )
    return False
