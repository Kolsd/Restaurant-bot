"""
tests/e2e/test_nps_reminder_lifecycle.py — E2E test: NPS 24h reminder full lifecycle.

What this exercises (no Anthropic, no LLM):
  1. Seed restaurant.
  2. Insert nps_waiting row backdated 25h (in the reminder window 24-48h).
  3. Drive scheduler._run_nps_reminders() → finds row, sends WA reminder
     (intercepted via wa_capture), marks reminded_at=NOW().
  4. Run scheduler again → no second WA send (idempotency).
  5. Insert another row backdated 50h (past cleanup threshold) → cleanup deletes it.

Skipped automatically when TEST_DATABASE_URL or ANTHROPIC_API_KEY is unset.
The actual test does NOT call Anthropic.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.e2e.conftest import (
    WACapture,
    seed_restaurant,
    truncate_e2e_data,
    _normalize_phone,
)
from app.services.logging import get_logger
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


CUSTOMER_PHONE_RAW = "+573009990801"
CUSTOMER_PHONE = _normalize_phone(CUSTOMER_PHONE_RAW)


@pytest_asyncio.fixture()
async def e2e_app(wa_capture):
    """Yields an httpx AsyncClient. wa_capture intercepts outbound Meta calls."""
    from app.main import app as fastapi_app
    from asgi_lifespan import LifespanManager

    async with LifespanManager(fastapi_app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://test",
            timeout=30.0,
        ) as client:
            yield client


async def _ensure_reminded_at_column(pool: asyncpg.Pool) -> None:
    """Ensure migration 0063 column exists on TEST_DATABASE_URL.

    test_pool fixture in conftest.py runs `alembic upgrade head` before yielding,
    so on a properly migrated test DB this is a no-op. Defensive guard for
    test DBs that haven't applied 0063 yet.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE nps_waiting ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ DEFAULT NULL"
        )


@pytest.mark.e2e_no_llm
@pytest.mark.asyncio
async def test_nps_reminder_full_lifecycle(
    test_pool: asyncpg.Pool,
    e2e_app: AsyncClient,
    wa_capture: WACapture,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    NPS 24h reminder flow:

      Seed: nps_waiting row(phone=customer, created_at=NOW()-25h)
      Action 1: scheduler._run_nps_reminders() → sends WA, sets reminded_at
      Assert: WA captured, reminded_at NOT NULL
      Action 2: scheduler runs again → no second WA (idempotency)
      Action 3: insert 50h-old row → cleanup deletes it
    """
    pool = test_pool

    # Scheduler needs a META_ACCESS_TOKEN to send the reminder WA. In E2E
    # we set a dummy — wa_capture intercepts the actual httpx call.
    monkeypatch.setenv("META_ACCESS_TOKEN", "e2e_dummy_token")

    await _ensure_reminded_at_column(pool)

    restaurant = await seed_restaurant(
        pool,
        name="E2E NPS Reminder Restaurant",
        bot_number_raw="+570E2ENPS",
        num_branches=1,
    )
    org_id = restaurant["id"]
    bot_number = restaurant["whatsapp_number"]

    # truncate_e2e_data already covers nps_responses; nps_waiting also needs
    # cleanup so prior runs don't pollute the scheduler scan.
    with bypass_tenant_scope("e2e_nps_reminder_cleanup"):
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM nps_waiting WHERE bot_number = $1",
                bot_number,
            )

    # ── Seed: 25h-old row in the reminder window (created_at TIMESTAMP, naive) ─
    created_at_25h = datetime.utcnow() - timedelta(hours=25)
    with bypass_tenant_scope("e2e_nps_reminder_seed"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO nps_waiting (phone, bot_number, org_id, created_at)
                   VALUES ($1, $2, $3, $4)""",
                CUSTOMER_PHONE, bot_number, org_id, created_at_25h,
            )

    # ── Drive the scheduler NPS reminder tick ──────────────────────────────────
    from app.services.scheduler import _run_nps_reminders

    await _run_nps_reminders()

    # ── Assert: WA reminder was sent ──────────────────────────────────────────
    captured = wa_capture.texts_to(CUSTOMER_PHONE_RAW)
    assert captured, (
        f"NPS scheduler did not send any reminder to {CUSTOMER_PHONE_RAW}. "
        f"Total captures: {len(wa_capture.all_texts())}. "
        "Either the scheduler scan failed, or the message wasn't fanned out."
    )
    msg = " ".join(captured)
    # Reminder message includes a survey nudge phrase
    assert "calificar" in msg.lower() or "1 al 5" in msg or "1 a 5" in msg, (
        f"NPS reminder message must mention rating, got: {msg!r}"
    )

    # ── Assert: reminded_at flipped to NOT NULL ───────────────────────────────
    with bypass_tenant_scope("e2e_nps_verify"):
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT reminded_at FROM nps_waiting "
                "WHERE phone=$1 AND bot_number=$2",
                CUSTOMER_PHONE, bot_number,
            )
            assert row is not None, "Seeded nps_waiting row disappeared"
            assert row["reminded_at"] is not None, (
                "scheduler._run_nps_reminders did not mark reminded_at — "
                "next tick will re-send the reminder."
            )

    # ── Idempotency: second scheduler run should not re-send ──────────────────
    sends_before = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))
    await _run_nps_reminders()
    sends_after = len(wa_capture.texts_to(CUSTOMER_PHONE_RAW))
    assert sends_after == sends_before, (
        f"Second scheduler run re-sent the reminder "
        f"({sends_before} → {sends_after}). reminded_at filter not working."
    )

    # ── Cleanup phase: 50h-old row should be deleted by next scheduler run ────
    expired_phone = "573009990888"
    created_at_50h = datetime.utcnow() - timedelta(hours=50)
    with bypass_tenant_scope("e2e_nps_seed_expired"):
        async with pool.acquire() as conn:
            await conn.execute("SET LOCAL ROLE mesio_app")
            await conn.execute(
                "SELECT set_config('app.org_id', $1::text, true)", str(org_id),
            )
            await conn.execute(
                """INSERT INTO nps_waiting (phone, bot_number, org_id, created_at)
                   VALUES ($1, $2, $3, $4)""",
                expired_phone, bot_number, org_id, created_at_50h,
            )

    # Verify it was inserted
    with bypass_tenant_scope("e2e_nps_verify_seed_expired"):
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM nps_waiting "
                "WHERE phone=$1 AND bot_number=$2",
                expired_phone, bot_number,
            )
            assert count == 1, "Expired-row seed failed"

    # Run scheduler — cleanup phase should delete the expired row
    await _run_nps_reminders()

    with bypass_tenant_scope("e2e_nps_verify_cleanup"):
        async with pool.acquire() as conn:
            count_after = await conn.fetchval(
                "SELECT COUNT(*) FROM nps_waiting "
                "WHERE phone=$1 AND bot_number=$2",
                expired_phone, bot_number,
            )
            assert count_after == 0, (
                f"Expired (50h-old) row was NOT cleaned up — "
                f"db_cleanup_expired_nps_waiting did not run or did not delete it."
            )

    log.info(
        "e2e.nps_reminder_lifecycle.passed",
        sends=sends_after,
        cleanup_succeeded=True,
    )
