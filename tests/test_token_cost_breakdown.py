"""
tests/test_token_cost_breakdown.py
==================================
The LLM bill, told truthfully.

Anthropic returns four token counters per response and prices them up to 50x
apart. Until migration 0094 the bot summed two of them into one column and
`cost_estimator` valued that sum at one blended $0.60/MTok, so the internal
margin dashboard reported a fraction of what a conversation really costs —
the exact number a flat per-sede price would be set from.

These tests pin both halves of the fix:
  - each kind of token is priced at its own rate, and a cached turn is
    materially more expensive than the old blended figure claimed;
  - the four counters reach the database and are read back per kind, while
    rows written before 0094 (no split recorded) keep the legacy rate
    instead of being re-priced with a split nobody measured.

Requirements: TEST_DATABASE_URL for the integration half (the pure pricing
tests run without a database).
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal

import asyncpg
import pytest

from tests.test_cost_metrics_repo import _ConnProxy, _PoolShim

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

_MILLION = 1_000_000


# ── Pricing (pure, no DB) ─────────────────────────────────────────────────────

def test_each_token_kind_is_priced_at_its_own_rate():
    """One million of each kind → that kind's list price, nothing blended."""
    from app.services.cost_estimator import estimate_cost_usd_breakdown as cost

    assert cost(input_tokens=_MILLION)       == Decimal("1.000000")
    assert cost(output_tokens=_MILLION)      == Decimal("5.000000")
    assert cost(cache_write_tokens=_MILLION) == Decimal("1.250000")
    assert cost(cache_read_tokens=_MILLION)  == Decimal("0.100000")

    # Additive: the four together cost the sum of the four.
    assert cost(_MILLION, _MILLION, _MILLION, _MILLION) == Decimal("7.350000")


def test_cached_turn_costs_several_times_the_old_blended_estimate():
    """The regression this migration exists for.

    A bot turn with a warm prompt cache: ~1k uncached input, ~300 output and
    ~8k served from cache. The old code recorded 1,300 tokens and priced them
    at $0.60/MTok; the real bill is the three counters at their own rates.
    """
    from app.services.cost_estimator import (
        estimate_cost_usd,
        estimate_cost_usd_breakdown,
    )

    real = estimate_cost_usd_breakdown(
        input_tokens=1_000, output_tokens=300, cache_read_tokens=8_000
    )
    old = estimate_cost_usd(1_000 + 300)

    assert real == Decimal("0.003300")
    assert old == Decimal("0.000780")
    # Not a rounding difference — the old figure was off by more than 4x.
    assert real > old * 4


def test_negative_counters_never_reduce_the_bill():
    """Garbage in must not produce a cost that flatters the margin."""
    from app.services.cost_estimator import estimate_cost_usd_breakdown as cost

    assert cost(-5_000_000, _MILLION, 0, 0) == Decimal("5.000000")
    assert cost(0, 0, 0, 0) == Decimal("0.000000")


def test_rate_override_falls_back_on_a_bad_value(monkeypatch):
    """A typo in a Railway variable must not zero out or crash the costing."""
    from app.services.cost_estimator import _rate

    monkeypatch.setenv("MESIO_TEST_RATE", "not-a-number")
    assert _rate("MESIO_TEST_RATE", "5.00") == Decimal("5.00")

    monkeypatch.setenv("MESIO_TEST_RATE", "0")
    assert _rate("MESIO_TEST_RATE", "5.00") == Decimal("5.00")

    monkeypatch.setenv("MESIO_TEST_RATE", "-3")
    assert _rate("MESIO_TEST_RATE", "5.00") == Decimal("5.00")

    monkeypatch.setenv("MESIO_TEST_RATE", "2.50")
    assert _rate("MESIO_TEST_RATE", "5.00") == Decimal("2.50")


# ── Integration ───────────────────────────────────────────────────────────────

_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — integration tests skipped",
)


@pytest.fixture
async def raw_pool():
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=3)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def usage_conn(raw_pool, monkeypatch):
    """Rolled-back connection with get_pool mocked, RLS enforced as mesio_app."""
    from app.services import database as db_module

    async with raw_pool.acquire() as conn:
        proxy = _ConnProxy(conn)
        shim = _PoolShim(proxy)

        async def _fake_get_pool():
            return shim

        monkeypatch.setattr(db_module, "get_pool", _fake_get_pool)

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("SET LOCAL ROLE mesio_app")
            yield proxy
        finally:
            await tx.rollback()


@pytest.fixture
async def org_id(usage_conn):
    row = await usage_conn.fetchrow(
        """INSERT INTO organizations (name, slug, subscription_plan)
           VALUES ('TokenCostOrg', 'token-cost-org', 'pro') RETURNING id""",
    )
    return row["id"]


async def _scope(conn, org: int):
    """Both GUCs, so the RLS WITH CHECK on subscription_usage is satisfied."""
    await conn.execute("SELECT set_config('app.org_id', $1::text, true)", str(org))
    await conn.execute("SELECT set_config('app.restaurant_id', $1::text, true)", str(org))


async def _seed(conn, org: int, day: date, *, total, inp=0, out=0, read=0, write=0):
    await conn.execute(
        """INSERT INTO subscription_usage
               (org_id, usage_date, total_tokens, input_tokens,
                output_tokens, cache_read_tokens, cache_write_tokens, orders_count)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 0)
           ON CONFLICT (org_id, usage_date) DO UPDATE
               SET total_tokens       = EXCLUDED.total_tokens,
                   input_tokens       = EXCLUDED.input_tokens,
                   output_tokens      = EXCLUDED.output_tokens,
                   cache_read_tokens  = EXCLUDED.cache_read_tokens,
                   cache_write_tokens = EXCLUDED.cache_write_tokens""",
        org, day, total, inp, out, read, write,
    )


@_db
@pytest.mark.asyncio
async def test_increment_records_every_counter_and_leaves_the_legacy_cap_alone(
    usage_conn, org_id
):
    """Two responses land in one daily row, each counter summed separately.

    `total_tokens` must keep counting uncached input + output ONLY: it feeds
    the per-day cap in db_check_usage_limits, and folding cache reads into it
    would tighten that cap several-fold for any org that has one configured.
    """
    from app.repositories.restaurant_repo import db_increment_token_usage
    from app.services.tenant_context import tenant_scope

    await _scope(usage_conn, org_id)

    with tenant_scope(org_id):
        await db_increment_token_usage(
            org_id, 1_300,
            input_tokens=1_000, output_tokens=300,
            cache_read_tokens=8_000, cache_write_tokens=0,
        )
        await db_increment_token_usage(
            org_id, 900,
            input_tokens=700, output_tokens=200,
            cache_read_tokens=9_000, cache_write_tokens=2_500,
        )

    row = await usage_conn.fetchrow(
        """SELECT total_tokens, input_tokens, output_tokens,
                  cache_read_tokens, cache_write_tokens
           FROM subscription_usage
           WHERE org_id = $1 AND usage_date = CURRENT_DATE""",
        org_id,
    )
    assert row["input_tokens"]       == 1_700
    assert row["output_tokens"]      == 500
    assert row["cache_read_tokens"]  == 17_000
    assert row["cache_write_tokens"] == 2_500
    assert row["total_tokens"]       == 2_200          # 1_700 + 500, no cache
    assert row["total_tokens"] < row["cache_read_tokens"]


@_db
@pytest.mark.asyncio
async def test_cost_is_read_per_kind_when_the_split_exists(usage_conn, org_id):
    """A row with the split is priced by kind — and costs more than the old way."""
    from app.repositories.cost_metrics_repo import db_restaurant_cost_detail
    from app.services.cost_estimator import (
        estimate_cost_usd,
        estimate_cost_usd_breakdown,
    )
    from app.services.tenant_context import bypass_tenant_scope

    day = date.today()
    await _scope(usage_conn, org_id)
    await _seed(usage_conn, org_id, day,
                total=1_300, inp=1_000, out=300, read=8_000, write=0)

    with bypass_tenant_scope("internal_cost_dashboard"):
        detail = await db_restaurant_cost_detail(org_id, day, day)

    expected = estimate_cost_usd_breakdown(
        input_tokens=1_000, output_tokens=300, cache_read_tokens=8_000
    )
    assert detail["totals"]["cost_usd"] == float(expected)
    assert detail["totals"]["cost_usd"] > float(estimate_cost_usd(1_300))


@_db
@pytest.mark.asyncio
async def test_rows_written_before_the_split_keep_the_legacy_rate(usage_conn, org_id):
    """No split recorded → blended rate, not a split invented after the fact."""
    from app.repositories.cost_metrics_repo import db_restaurant_cost_detail
    from app.services.cost_estimator import estimate_cost_usd
    from app.services.tenant_context import bypass_tenant_scope

    day = date.today() - timedelta(days=2)
    await _scope(usage_conn, org_id)
    await _seed(usage_conn, org_id, day, total=50_000)   # pre-0094 shape

    with bypass_tenant_scope("internal_cost_dashboard"):
        detail = await db_restaurant_cost_detail(org_id, day, day)

    assert detail["totals"]["cost_usd"] == float(estimate_cost_usd(50_000))


@_db
@pytest.mark.asyncio
async def test_a_period_spanning_both_eras_adds_the_two_up(usage_conn, org_id):
    """One legacy day + one split day = legacy cost + split cost, exactly."""
    from app.repositories.cost_metrics_repo import db_restaurant_cost_detail
    from app.services.cost_estimator import (
        estimate_cost_usd,
        estimate_cost_usd_breakdown,
    )
    from app.services.tenant_context import bypass_tenant_scope

    old_day = date.today() - timedelta(days=3)
    new_day = date.today() - timedelta(days=1)
    await _scope(usage_conn, org_id)
    await _seed(usage_conn, org_id, old_day, total=50_000)
    await _seed(usage_conn, org_id, new_day,
                total=1_300, inp=1_000, out=300, read=8_000, write=0)

    with bypass_tenant_scope("internal_cost_dashboard"):
        detail = await db_restaurant_cost_detail(org_id, old_day, new_day)

    expected = estimate_cost_usd(50_000) + estimate_cost_usd_breakdown(
        input_tokens=1_000, output_tokens=300, cache_read_tokens=8_000
    )
    assert detail["totals"]["cost_usd"] == float(expected)
    assert detail["totals"]["tokens"] == 51_300
