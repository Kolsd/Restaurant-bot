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

    # Match production's chat.py:284 behavior: Meta webhook normalizes bot_number
    # via _normalize_phone (strips '+' and spaces) BEFORE calling chat().
    # detect_table_context and a few other internal lookups assume this is already
    # normalized. Scenarios use "+57TESTBOT1" for readability, we strip here.
    normalized_bot = _norm(scenario.bot_number)
    normalized_user = _norm(scenario.user_phone)

    # ── Resolve org_id for tenant_scope wrapping (Wave 2 / Rule 14) ───────────
    # Production's inbox_worker._handle_meta_whatsapp wraps agent.chat in
    # tenant_scope(org["id"]) after resolving from bot_number. The sim bypasses
    # the inbox so it must do the same here — otherwise every repo call inside
    # agent.chat raises TenantNotSetError. Cross-tenant lookup needs a bypass.
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

        # Apply table_hint to first turn: inject a [table_id:...] bracketed tag,
        # matching exactly how a real QR code scan appends the tag to the URL.
        # Production seeds tables as id="sim_mesa_N" in seed.py, so that's what
        # the QR would carry. The text prefix "Estoy en la mesa N." is ALSO kept
        # for realism — but with `allow_manual_table_number=False` in features
        # (the safe default), only the [table_id:...] tag actually creates the
        # session. Pure text would be rejected, exactly as in production.
        if turn_idx == 0 and scenario.table_hint is not None:
            qr_tag = f"[table_id:sim_mesa_{scenario.table_hint}]"
            text_prefix = f"Estoy en la mesa {scenario.table_hint}. "
            # Prefix both so the bot gets: "<QR tag>\nEstoy en la mesa N. <original>"
            user_text = f"{qr_tag}\n{text_prefix}{user_text}"
            log.debug(
                "runner.table_hint_applied",
                scenario_id=scenario.id,
                table_hint=scenario.table_hint,
                user_text_preview=user_text[:120],
            )

        bot_reply = "[SILENT]"
        raw_result: Any = None
        latency_ms = 0

        try:
            t_start = time.monotonic()
            # Rule 14: agent.chat must run inside tenant_scope(org_id). In
            # production, inbox_worker does this. The sim replicates that here.
            with tenant_scope(org_id):
                raw_result = await _agent.chat(
                    user_phone=normalized_user,
                    user_message=user_text,
                    bot_number=normalized_bot,
                    meta_phone_id="",
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
            webhook_inbox_stats={},
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
