"""
Reservation deposit payment service — Wompi prepayment link generation
and webhook confirmation for the `reservation_deposits` feature flag.
"""

import os
import hashlib
from decimal import Decimal

from app.services.money import to_decimal, quantize_money
from app.repositories import reservation_deposits_repo as deposits_repo
from app.services.logging import get_logger

log = get_logger(__name__)

WOMPI_PUBLIC_KEY       = os.getenv("WOMPI_PUBLIC_KEY", "")
WOMPI_INTEGRITY_SECRET = os.getenv("WOMPI_INTEGRITY_SECRET", "")
APP_DOMAIN             = os.getenv("APP_DOMAIN", "mesioai.com")

# Zero-decimal currencies (no cents multiplier needed)
_ZERO_DECIMAL_CURRENCIES = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}


async def generate_deposit_link(
    reservation_id: int,
    amount: Decimal,
    currency: str = "COP",
) -> str:
    """
    Generate a Wompi checkout link for a reservation deposit and persist the
    deposit record.  Returns the full payment URL string.
    """
    quantized = quantize_money(amount, currency)

    # Wompi expects amount-in-cents; for zero-decimal currencies the
    # "cents" value equals the face amount (already integer-like).
    if currency in _ZERO_DECIMAL_CURRENCIES:
        amount_cents = int(quantized)
    else:
        amount_cents = int(quantized * 100)

    reference = f"dep_{reservation_id}"

    # Wompi integrity signature: reference + amount_cents + currency + secret
    integrity_str = f"{reference}{amount_cents}{currency}{WOMPI_INTEGRITY_SECRET}"
    signature = hashlib.sha256(integrity_str.encode()).hexdigest()

    redirect_url = f"https://{APP_DOMAIN}/api/reservations/{reservation_id}"

    payment_url = (
        f"https://checkout.wompi.co/p/?public-key={WOMPI_PUBLIC_KEY}"
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
    Confirms the deposit row and transitions the reservation to confirmed status.
    Returns True if the deposit was successfully confirmed.
    """
    if not reference.startswith("dep_"):
        return False

    try:
        reservation_id = int(reference.replace("dep_", ""))
    except ValueError:
        log.warning("deposit.bad_reference", reference=reference)
        return False

    from app.services import database as db  # noqa: PLC0415 — lazy, avoids cycle

    result = await deposits_repo.db_confirm_deposit(reservation_id, transaction_id)
    if result:
        await db.db_confirm_reservation(reservation_id)
        log.info(
            "deposit.confirmed_and_reservation_activated",
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
