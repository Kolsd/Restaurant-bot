"""
AI Daily Insight generator — Tier 4a.

Builds a prompt from current restaurant signals and calls Claude Haiku to
produce a 2-3 sentence actionable Spanish insight for the dashboard banner.

Results are cached in Redis with a 24-hour TTL keyed by org_id + UTC date so
the LLM is called at most once per restaurant per day.

Error strategy:
  - Missing API key      → return signals-only payload (no crash)
  - Anthropic API error  → log + return cached if available, else signals-only
  - Redis down           → skip cache, compute fresh every call (warn once/min)
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime, timezone
from typing import Any

from app.services.logging import get_logger

log = get_logger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

_LLM_MODEL = "claude-haiku-4-5-20251001"
_LLM_MAX_TOKENS = 400
_LLM_TIMEOUT = 15.0  # seconds
_CACHE_TTL = 86_400  # 24 hours


# ── helpers ──────────────────────────────────────────────────────────────────

def _cache_key(org_id: int) -> str:
    today = date.today().isoformat()
    return f"mesio:ai_insight:{org_id}:{today}"


def _signals_payload_only(signals: dict) -> dict:
    """Return the error shape when LLM is unavailable but signals were gathered."""
    return {
        "enabled": True,
        "error": "llm_unavailable",
        "signals": signals,
    }


async def _gather_signals(org_id: int, location_id: int) -> dict:
    """Gather the numeric signals used for the prompt and the response payload."""
    from datetime import timedelta

    from app.repositories import stats_repo
    from app.repositories.marketing_repo import get_at_risk_customers

    today = date.today()
    week_start = str(today - timedelta(days=6))
    today_str = str(today)

    # Run all repo calls concurrently — they each open their own tenant_connection
    (channel_data, payment_data, top_dishes_data, at_risk_rows, inventory_data) = (
        await asyncio.gather(
            stats_repo.db_sales_by_channel(org_id, week_start, today_str),
            stats_repo.db_payment_status(org_id, today_str, today_str),
            stats_repo.db_top_dishes(org_id, week_start, today_str, limit=3),
            get_at_risk_customers(restaurant_id=org_id, limit=200),
            stats_repo.db_inventory_critical(org_id),
            return_exceptions=True,
        )
    )

    # Safely extract even if some calls returned exceptions (best-effort signals)
    sales_7d = 0
    if not isinstance(channel_data, Exception):
        sales_7d = channel_data.get("total", 0)

    pending_today = 0
    if not isinstance(payment_data, Exception):
        for b in payment_data.get("buckets", []):
            if b["key"] == "pending":
                pending_today = b["count"]

    top_dish = None
    if not isinstance(top_dishes_data, Exception):
        dishes = top_dishes_data.get("dishes", [])
        if dishes:
            top_dish = dishes[0].get("name")

    at_risk_count = 0
    if not isinstance(at_risk_rows, Exception):
        at_risk_count = len(at_risk_rows)

    inventory_alerts = 0
    if not isinstance(inventory_data, Exception):
        inventory_alerts = len(inventory_data.get("alerts", []))

    return {
        "sales_7d_total": sales_7d,
        "pending_orders_today": pending_today,
        "top_dish": top_dish,
        "inventory_alerts_count": inventory_alerts,
        "at_risk_customers_count": at_risk_count,
    }


def _build_prompt(signals: dict) -> str:
    lines = [
        f"Ventas últimos 7 días: ${signals['sales_7d_total']:,} COP",
        f"Órdenes pendientes hoy: {signals['pending_orders_today']}",
    ]
    if signals.get("top_dish"):
        lines.append(f"Plato más vendido (7d): {signals['top_dish']}")
    if signals["inventory_alerts_count"] > 0:
        lines.append(f"Alertas de inventario críticas/advertencias: {signals['inventory_alerts_count']}")
    if signals["at_risk_customers_count"] > 0:
        lines.append(f"Clientes frecuentes en riesgo (sin comprar ≥21 días): {signals['at_risk_customers_count']}")

    data_block = "\n".join(lines)
    return (
        f"Aquí están los indicadores clave del restaurante:\n\n{data_block}\n\n"
        "Genera un insight de 2-3 oraciones en español colombiano, directo y accionable para el dueño. "
        "Incluye números específicos donde sea útil. No repitas todos los datos — "
        "resalta lo más importante y termina con una acción concreta."
    )


async def _call_llm(prompt: str, org_id: int) -> str | None:
    """Call Anthropic and return the insight text, or None on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ai_insight.no_api_key", org_id=org_id)
        return None

    try:
        from anthropic import AsyncAnthropic  # noqa: PLC0415
        client = AsyncAnthropic(api_key=api_key)
        resp = await asyncio.wait_for(
            client.messages.create(
                model=_LLM_MODEL,
                max_tokens=_LLM_MAX_TOKENS,
                system=(
                    "Eres un asistente de restaurante que genera insights breves y accionables. "
                    "2-3 oraciones en español colombiano. Incluye números específicos."
                ),
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=_LLM_TIMEOUT,
        )
        tokens_in = resp.usage.input_tokens if resp.usage else 0
        tokens_out = resp.usage.output_tokens if resp.usage else 0
        log.info("ai_insight.llm_call", org_id=org_id, tokens_in=tokens_in, tokens_out=tokens_out)
        return resp.content[0].text
    except asyncio.TimeoutError:
        log.warning("ai_insight.llm_timeout", org_id=org_id)
        return None
    except Exception as exc:
        log.error("ai_insight.llm_error", org_id=org_id, error=str(exc))
        return None


async def generate_daily_insight(org_id: int, location_id: int) -> dict[str, Any]:
    """Top-level function called from the route.

    Returns the full insight payload (cached or fresh).
    Always returns a valid dict — never raises.
    """
    from app.services import redis_client as _rc  # noqa: PLC0415

    cache_key = _cache_key(org_id)

    # ── cache read ────────────────────────────────────────────────────────────
    cached_raw: str | None = None
    try:
        r = await _rc.get_redis()
        if r:
            cached_raw = await r.get(cache_key)
    except Exception as exc:
        log.warning("ai_insight.cache_read_error", org_id=org_id, error=str(exc))

    if cached_raw:
        try:
            return json.loads(cached_raw)
        except Exception:
            pass  # malformed cache → recompute

    # ── gather signals ────────────────────────────────────────────────────────
    try:
        signals = await _gather_signals(org_id, location_id)
    except Exception as exc:
        log.error("ai_insight.signals_error", org_id=org_id, error=str(exc))
        return _signals_payload_only({})

    # ── call LLM ─────────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _signals_payload_only(signals)

    prompt = _build_prompt(signals)
    insight_text = await _call_llm(prompt, org_id)

    if insight_text is None:
        # LLM unavailable — return signals only (no crash)
        return _signals_payload_only(signals)

    # ── build response ────────────────────────────────────────────────────────
    payload: dict[str, Any] = {
        "enabled": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "insight_text": insight_text,
        "signals": signals,
    }

    # ── cache write ───────────────────────────────────────────────────────────
    try:
        r = await _rc.get_redis()
        if r:
            await r.set(cache_key, json.dumps(payload), ex=_CACHE_TTL)
    except Exception as exc:
        log.warning("ai_insight.cache_write_error", org_id=org_id, error=str(exc))

    return payload
