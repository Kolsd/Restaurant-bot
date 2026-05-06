"""
app/services/cost_estimator.py

Pure cost-estimation helpers for Anthropic/LLM token spend.
No DB access — safe to call from routes, repos, or tests directly.

Pricing model (measured 2026-05-05):
  $0.17 USD for 284,778 tokens with high cache hit → ~$0.597 per M tokens.
  Rounded to $0.60/Mtok blended rate (cache-adjusted, conservative).

Both rates are tunable via env vars so Mesio can update without a deploy:
  MESIO_TOKEN_COST_USD_PER_MTOK  — override blended $/Mtok
  MESIO_USD_TO_COP_RATE          — override exchange rate

All arithmetic is Decimal end-to-end.
Float appears ONLY at the JSON boundary (callers do float(...) there).

# TODO: cross-check with Anthropic Admin API for tampering detection
#   (PM will provide org_id once available — Anthropic charges per-org,
#   so we can diff platform-wide spend vs. our internal token sum to
#   detect log omissions or replay attacks).
"""

from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_EVEN

# ── Pricing constants ─────────────────────────────────────────────────────────

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


# ── Public helpers ────────────────────────────────────────────────────────────


def estimate_cost_usd(tokens: int) -> Decimal:
    """Return estimated cost in USD for *tokens* with 6 decimal places.

    Uses the blended cached-input rate (TOKEN_COST_USD_PER_MTOK_BLENDED).
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
