"""
tests/ai_sim/runner.py — Scenario orchestrator for the AI simulation harness.

Drives multi-turn conversations through agent.chat() against a real DB,
collects transcripts, snapshots DB state, runs the LLM judge, and checks
hard DB assertions.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import asyncpg

from tests.ai_sim.types import (
    AssertionResult,
    DBSnapshot,
    JudgeVerdict,
    Scenario,
    ScenarioResult,
    TurnResult,
)
from tests.ai_sim.seed import (
    truncate_test_data,
    reset_state_store_fallbacks,
    SIM_BOT_NUMBER,
)
from tests.ai_sim import assertions as _assertions
from tests.ai_sim import judge as _judge
from app.services.logging import get_logger

log = get_logger(__name__)


async def _sit_at_table(org_id: int, phone: str, bot_number: str, table_id: str) -> None:
    """Open the diner's table session, as a QR scan does."""
    from app.services import database as db  # noqa: PLC0415
    from app.services.tenant_context import tenant_scope  # noqa: PLC0415

    with tenant_scope(org_id):
        table = await db.db_get_table_by_id(table_id)
        if not table:
            raise RuntimeError(f"runner: table {table_id!r} not seeded")
        if not await db.db_get_active_session(phone, bot_number):
            await db.db_create_table_session(
                phone, bot_number, table["id"], table["name"],
                org_id=table.get("org_id"), location_id=table.get("location_id"),
            )


async def run_scenario(scenario: Scenario, pool: asyncpg.Pool) -> ScenarioResult:
    """Run one scenario end-to-end and return a complete ScenarioResult.

    Steps:
    1. Truncate volatile tables + re-seed subscription_usage
    2. Reset state_store in-process fallbacks
    3. Execute each scripted turn via agent.chat()
    4. Snapshot DB state
    5. Run LLM judge
    6. Run hard DB assertions
    7. Compute passed flag and return
    """
    log.info("runner.scenario_start", scenario_id=scenario.id, suite=scenario.suite)

    # ── Step 1 & 2: Clean DB + state ──────────────────────────────────────────
    async with pool.acquire() as conn:
        await truncate_test_data(conn)
    reset_state_store_fallbacks()

    # ── Step 3: Execute turns ─────────────────────────────────────────────────
    # Import agent lazily to avoid circular imports at module load time
    from app.services import agent as _agent  # noqa: PLC0415
    from app.services.database import _normalize_phone as _norm  # noqa: PLC0415
    from app.services.tenant_context import tenant_scope, bypass_tenant_scope  # noqa: PLC0415

    # Scenarios use "+57TESTBOT1" for readability; the stored key is normalized
    # (no '+' / spaces), and the internal lookups assume the normalized form.
    normalized_bot = _norm(scenario.bot_number)
    normalized_user = _norm(scenario.user_phone)

    # ── Resolve org_id for tenant_scope wrapping (Wave 2 / Rule 14) ───────────
    # POST /api/diner/chat runs agent.chat inside tenant_scope(org_id); the sim
    # must do the same or every repo call inside it raises TenantNotSetError.
    # Cross-tenant lookup needs a bypass.
    async with pool.acquire() as conn:
        with bypass_tenant_scope("ai_sim_resolve_org_for_scope"):
            org_id = await conn.fetchval(
                "SELECT id FROM organizations WHERE whatsapp_number = $1",
                normalized_bot,
            )
    if org_id is None:
        raise RuntimeError(
            f"runner: could not resolve org_id for bot_number={normalized_bot!r}. "
            f"Did seed_restaurant run?"
        )

    transcript: list[TurnResult] = []
    total_latency_ms = 0
    tokens_estimate = 0

    for turn_idx, scripted_turn in enumerate(scenario.turns):
        user_text = scripted_turn.user

        # table_hint: the diner scanned the QR of table sim_mesa_N (seed.py)
        # before writing — open that table session the way
        # POST /api/diner/session + diner_chat do. (Until 2026-09-25 the sim
        # appended a "[table_id:...]" tag to the text instead; the bot no
        # longer reads table ids from message text.)
        if turn_idx == 0 and scenario.table_hint is not None:
            await _sit_at_table(
                org_id, normalized_user, normalized_bot, f"sim_mesa_{scenario.table_hint}",
            )
            log.debug(
                "runner.table_hint_applied",
                scenario_id=scenario.id,
                table_hint=scenario.table_hint,
            )

        bot_reply = "[SILENT]"
        raw_result: Any = None
        latency_ms = 0

        try:
            t_start = time.monotonic()
            # Rule 14: agent.chat must run inside tenant_scope(org_id), as
            # POST /api/diner/chat does.
            with tenant_scope(org_id):
                raw_result = await _agent.chat(
                    user_phone=normalized_user,
                    user_message=user_text,
                    bot_number=normalized_bot,
                )
            latency_ms = int((time.monotonic() - t_start) * 1000)

            if raw_result is None:
                bot_reply = "[SILENT]"
            else:
                msg = raw_result.get("message") if isinstance(raw_result, dict) else None
                bot_reply = msg if msg else "[SILENT]"

        except Exception as exc:  # noqa: BLE001
            latency_ms = int((time.monotonic() - t_start) * 1000) if 't_start' in dir() else 0
            bot_reply = f"[ERROR: {type(exc).__name__}: {exc}]"
            log.exception(
                "runner.turn_error",
                scenario_id=scenario.id,
                turn_idx=turn_idx,
                error=str(exc),
            )

        turn_result = TurnResult(
            user=user_text,
            bot=bot_reply,
            raw_result=raw_result,
            latency_ms=latency_ms,
        )
        transcript.append(turn_result)
        total_latency_ms += latency_ms
        tokens_estimate += len(user_text) + len(bot_reply)

        log.info(
            "runner.turn_complete",
            scenario_id=scenario.id,
            turn=turn_idx + 1,
            latency_ms=latency_ms,
            bot_preview=bot_reply[:80],
        )

    # ── Step 4: Snapshot DB state ─────────────────────────────────────────────
    # Use the already-normalized bot/user (agent.chat wrote rows using these).
    db_state: DBSnapshot
    try:
        async with pool.acquire() as conn:
            db_state = await _assertions.snapshot_db_state(
                conn,
                bot_number=normalized_bot,
                user_phone=normalized_user,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("runner.snapshot_failed", scenario_id=scenario.id, error=str(exc))
        db_state = DBSnapshot(
            table_orders=[],
            orders=[],
            carts=[],
            waiter_alerts=[],
            reservations=[],
            conversations=[],
            subscription_usage={},
        )

    # ── Step 5: LLM judge ─────────────────────────────────────────────────────
    judge_verdict: JudgeVerdict
    try:
        judge_verdict = await _judge.evaluate(scenario, transcript, db_state)
    except Exception as exc:  # noqa: BLE001
        log.exception("runner.judge_failed", scenario_id=scenario.id, error=str(exc))
        judge_verdict = JudgeVerdict(
            verdict="ERROR",
            score=0,
            violated_rules=[],
            critical_failures=[f"Judge evaluation raised exception: {exc}"],
            explanation=str(exc),
        )

    # ── Step 6: Hard DB assertions ────────────────────────────────────────────
    asserts: AssertionResult
    try:
        asserts = _assertions.check(scenario.expected_state, db_state)
    except Exception as exc:  # noqa: BLE001
        log.exception("runner.assertions_failed", scenario_id=scenario.id, error=str(exc))
        asserts = AssertionResult(
            all_passed=False,
            failures=[f"Assertion check raised exception: {exc}"],
        )

    # ── Step 7: Compute final pass/fail ───────────────────────────────────────
    passed = (judge_verdict.verdict == "PASS") and asserts.all_passed

    log.info(
        "runner.scenario_complete",
        scenario_id=scenario.id,
        passed=passed,
        verdict=judge_verdict.verdict,
        score=judge_verdict.score,
        assertion_failures=len(asserts.failures),
        total_latency_ms=total_latency_ms,
    )

    return ScenarioResult(
        scenario=scenario,
        transcript=transcript,
        db_state=db_state,
        judge=judge_verdict,
        asserts=asserts,
        passed=passed,
        total_latency_ms=total_latency_ms,
        tokens_estimate=tokens_estimate,
    )


async def run_all(
    scenarios: list[Scenario],
    pool: asyncpg.Pool,
    fail_fast: bool = False,
) -> list[ScenarioResult]:
    """Run all scenarios sequentially. If fail_fast=True, stop on first failure."""
    results: list[ScenarioResult] = []

    for scenario in scenarios:
        result = await run_scenario(scenario, pool)
        results.append(result)

        if fail_fast and not result.passed:
            log.warning(
                "runner.fail_fast_triggered",
                scenario_id=scenario.id,
                remaining=len(scenarios) - len(results),
            )
            break

    passed_count = sum(1 for r in results if r.passed)
    log.info(
        "runner.all_complete",
        total=len(results),
        passed=passed_count,
        failed=len(results) - passed_count,
    )

    return results
