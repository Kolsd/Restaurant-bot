"""
tests/ai_sim/conftest.py — Isolated pytest fixtures for the AI simulation suite.

IMPORTANT: This file does NOT import from tests/conftest.py. That file is
contaminated with AsyncMock / MagicMock patches that would defeat the purpose of
real end-to-end simulation.

Fixtures provided:
  test_db_pool  (module-scoped)  — asyncpg pool, validates DATABASE_URL
  clean_db      (function-scoped) — truncate + re-seed + reset fallbacks
"""
from __future__ import annotations

import os
import warnings

import asyncpg
import pytest
import pytest_asyncio

from tests.ai_sim.seed import (
    seed_restaurant,
    truncate_test_data,
    reset_state_store_fallbacks,
)
from app.services.logging import get_logger

log = get_logger(__name__)


# ── Pool fixture ──────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="module")
async def test_db_pool():
    """Module-scoped asyncpg pool.

    Reads DATABASE_URL from environment. Warns (does NOT skip) if the URL does
    not contain "test" or "sim" so developers are alerted about running against
    a production database.

    Does NOT run alembic — that is the job of run_ai_sim.py entry point.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL not set — skipping AI simulation tests")

    # Normalize postgres:// → postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    lower_url = url.lower()
    if "test" not in lower_url and "sim" not in lower_url:
        warnings.warn(
            f"DATABASE_URL does not contain 'test' or 'sim'. "
            f"Running simulation against what may be a production database. "
            f"URL preview: {url[:60]}...",
            stacklevel=2,
        )

    pool = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=10,
        command_timeout=60,
    )
    log.info("conftest.pool_created", min_size=1, max_size=10)

    yield pool

    await pool.close()
    log.info("conftest.pool_closed")


# ── Clean DB fixture ──────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def clean_db(test_db_pool):
    """Function-scoped fixture: truncate volatile tables, re-seed, reset fallbacks.

    Yields the seed info dict: {"restaurant_id": int, "bot_number": str}

    NOTE: Does NOT use a transaction-rollback approach (like tests/conftest.py
    db_conn does). agent.chat() acquires its own pool connections and commits
    independently — those commits would be invisible inside our transaction.
    TRUNCATE between tests is the correct isolation strategy here.
    """
    async with test_db_pool.acquire() as conn:
        # First ensure the restaurant is seeded (idempotent)
        seed_info = await seed_restaurant(conn)
        # Then truncate volatile tables and re-seed subscription_usage
        await truncate_test_data(conn)

    # Reset in-process fallback state so NPS/checkout/cart from prior scenario
    # don't bleed into this one
    reset_state_store_fallbacks()

    log.info("conftest.clean_db_ready", seed_info=seed_info)
    yield seed_info
