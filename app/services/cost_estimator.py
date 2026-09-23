"""
app/services/cost_estimator.py

Pure cost-estimation helpers for Anthropic/LLM token spend.
No DB access — safe to call from routes, repos, or tests directly.

Pricing model — per KIND of token, because they differ by up to 50x.
List prices for the bot's model (Haiku 4.5, a closed PM decision):

  input        $1.00 /MTok   uncached input
  output       $5.00 /MTok   generated tokens
  cache write  $1.25 /MTok   written to the prompt cache (1.25x input)
  cache read   $0.10 /MTok   served from the prompt cache (0.1x input)

`estimate_cost_usd_breakdown` is the function to use. The single blended
rate below survives ONLY to price `subscription_usage` rows written before
migration 0094, which never recorded the split — see the warning on
`estimate_cost_usd`.

Every rate is tunable via env vars so Mesio can update without a deploy:
  MESIO_TOKEN_COST_USD_INPUT       — $/MTok, uncached input
  MESIO_TOKEN_COST_USD_OUTPUT      — $/MTok, output
  MESIO_TOKEN_COST_USD_CACHE_WRITE — $/MTok, cache creation
  MESIO_TOKEN_COST_USD_CACHE_READ  — $/MTok, cache read
  MESIO_TOKEN_COST_USD_PER_MTOK    — override the legacy blended $/Mtok
  MESIO_USD_TO_COP_RATE            — override exchange rate

All arithmetic is Decimal end-to-end.
Float appears ONLY at the JSON boundary (callers do float(...) there).

# TODO: cross-check with Anthropic Admin API for tampering detection
#   (PM will provide org_id once available — Anthropic charges per-org,
#   so we can diff platform-wide spend vs. our internal token sum to
#   detect log omissions or replay attacks).
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

# ── Pricing constants ─────────────────────────────────────────────────────────

def _rate(env_var: str, default: str) -> Decimal:
    """Read a $/MTok rate from the environment, falling back to list price.

    An unparseable or non-positive override is ignored rather than raising:
    a typo in a Railway variable must not take the bot down, and a rate of
    zero would silently report infinite margin.
    """
    raw = os.getenv(env_var, "").strip()
    if not raw:
        return Decimal(default)
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal(default)
    return value if value > 0 else Decimal(default)


# Per-kind list prices (Haiku 4.5).
COST_USD_PER_MTOK_INPUT: Decimal       = _rate("MESIO_TOKEN_COST_USD_INPUT", "1.00")
COST_USD_PER_MTOK_OUTPUT: Decimal      = _rate("MESIO_TOKEN_COST_USD_OUTPUT", "5.00")
COST_USD_PER_MTOK_CACHE_WRITE: Decimal = _rate("MESIO_TOKEN_COST_USD_CACHE_WRITE", "1.25")
COST_USD_PER_MTOK_CACHE_READ: Decimal  = _rate("MESIO_TOKEN_COST_USD_CACHE_READ", "0.10")

# Legacy blended rate — pre-0094 rows only. See estimate_cost_usd().
_ENV_RATE = os.getenv("MESIO_TOKEN_COST_USD_PER_MTOK", "").strip()
TOKEN_COST_USD_PER_MTOK_BLENDED: Decimal = (
    Decimal(_ENV_RATE) if _ENV_RATE else Decimal("0.60")
)

_ENV_FX = os.getenv("MESIO_USD_TO_COP_RATE", "").strip()
USD_TO_COP_RATE: Decimal = (
    Decimal(_ENV_FX) if _ENV_FX else Decimal("4000")
)

# One million tokens as a Decimal constant
_ONE_MILLION = Decimal("1_000_000")

ZERO_USD = Decimal("0")


# ── Public helpers ────────────────────────────────────────────────────────────


def estimate_cost_usd_breakdown(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Return the estimated USD cost of one response's four token counters.

    This is the accurate estimator: each counter is priced at its own rate,
    which is the whole point — on a cached bot turn the cache-read counter
    holds most of the tokens and a tenth of the cost, while the output
    counter holds few tokens and five times the per-token price. Collapsing
    them into one number (as the pre-0094 code did) is wrong in both
    directions at once.

    Negative counters are treated as zero; a caller passing garbage should
    not produce a negative cost that flatters the margin.

    >>> estimate_cost_usd_breakdown(output_tokens=1_000_000)
    Decimal('5.000000')
    >>> estimate_cost_usd_breakdown(cache_read_tokens=1_000_000)
    Decimal('0.100000')
    >>> estimate_cost_usd_breakdown(1_000, 300, 8_000, 0)  # a typical bot turn
    Decimal('0.003300')
    """
    pairs = (
        (input_tokens, COST_USD_PER_MTOK_INPUT),
        (output_tokens, COST_USD_PER_MTOK_OUTPUT),
        (cache_read_tokens, COST_USD_PER_MTOK_CACHE_READ),
        (cache_write_tokens, COST_USD_PER_MTOK_CACHE_WRITE),
    )
    total = ZERO_USD
    for count, rate in pairs:
        if count and count > 0:
            total += (Decimal(count) / _ONE_MILLION) * rate
    return total.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def estimate_cost_cop_breakdown(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """`estimate_cost_usd_breakdown` converted to COP, rounded to the peso."""
    return usd_to_cop(
        estimate_cost_usd_breakdown(
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        )
    )


def estimate_cost_usd(tokens: int) -> Decimal:
    """LEGACY. Price an undifferentiated token count at one blended rate.

    Only for `subscription_usage` rows written before migration 0094, which
    recorded a single sum and no split. It UNDERSTATES cost — the blended
    $0.60/MTok sits below even the uncached input price — and it is kept at
    that value on purpose: re-pricing history with a rate we guess today
    would replace one wrong number with a different wrong number. New code
    calls `estimate_cost_usd_breakdown`.

    Returns Decimal("0.000000") for zero or negative token counts.

    >>> estimate_cost_usd(1_000_000)  # 1 M tokens → $0.60
    Decimal('0.600000')
    >>> estimate_cost_usd(284_778)    # measured session
    Decimal('0.170867')
    """
    if tokens <= 0:
        return Decimal("0.000000")
    raw = (Decimal(tokens) / _ONE_MILLION) * TOKEN_COST_USD_PER_MTOK_BLENDED
    return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def estimate_cost_cop(tokens: int) -> Decimal:
    """Return estimated cost in COP, quantized to the nearest peso.

    >>> estimate_cost_cop(1_000_000)  # 1 M tokens → $0.60 × 4000 = $2400 COP
    Decimal('2400')
    """
    usd = estimate_cost_usd(tokens)
    return usd_to_cop(usd)


def usd_to_cop(usd: Decimal) -> Decimal:
    """Convert a USD Decimal amount to COP, rounded to nearest peso.

    >>> usd_to_cop(Decimal("0.60"))
    Decimal('2400')
    """
    raw = usd * USD_TO_COP_RATE
    return raw.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
