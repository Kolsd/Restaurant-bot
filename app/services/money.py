"""
Money helpers. Single source of truth for financial arithmetic.

Rules:
- Never use float for money. Always Decimal.
- Quantization depends on currency (COP/CLP have 0 decimals, most others 2).
- Rounding: ROUND_HALF_EVEN (banker's rounding) — matches accounting norms and DIAN.
- Conversion from untrusted input (JSON, user text) goes through to_decimal().
"""

from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation
from typing import Iterable, Any

ZERO = Decimal("0")

# Currencies with no fractional units
_ZERO_DECIMAL_CURRENCIES = {"COP", "CLP", "JPY", "KRW", "VND", "PYG", "ISK"}


def currency_exponent(currency: str | None) -> int:
    if not currency:
        return 2
    return 0 if currency.upper() in _ZERO_DECIMAL_CURRENCIES else 2


def to_decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    """Safely coerce anything to Decimal. Accepts Decimal, int, str, float (via str), None."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Go through str to avoid binary float representation leaking in
        return Decimal(str(value))
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return default
        try:
            return Decimal(s)
        except InvalidOperation:
            return default
    # Last resort: str() it
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def quantize_money(value: Any, currency: str | None = None) -> Decimal:
    """Round to the correct number of decimals for the currency."""
    d = to_decimal(value)
    exp = currency_exponent(currency)
    if exp == 0:
        return d.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


def format_money_es(value: Any, currency: str | None = None) -> str:
    """Customer-facing amount in Spanish/LatAm style: "$ 75.400" for COP,
    "$ 1.234,50" for a two-decimal currency.

    Matches what the diner pages render with Intl es-CO, so a server-side
    message ("El monto indicado no cubre el total del pedido ($ 75.400)")
    reads the same as the totals printed right above it. Python's `:,`
    format spec gives the US style ("$75,400"), which a Colombian customer
    reads as seventy-five pesos with a decimal part.
    """
    amount = quantize_money(value, currency)
    exp = currency_exponent(currency)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if exp == 0:
        body = f"{int(amount):,}".replace(",", ".")
    else:
        whole, frac = f"{amount:.2f}".split(".")
        body = f"{int(whole):,}".replace(",", ".") + "," + frac
    return f"{sign}$ {body}"


def money_sum(values: Iterable[Any]) -> Decimal:
    total = ZERO
    for v in values:
        total += to_decimal(v)
    return total


def money_mul(a: Any, b: Any) -> Decimal:
    return to_decimal(a) * to_decimal(b)
