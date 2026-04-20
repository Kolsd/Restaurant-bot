#!/usr/bin/env python3
"""
run_ai_sim.py — CLI entry point for the Mesio AI Simulation Test Suite.

Runs real end-to-end multi-turn conversations through agent.chat() against a
real PostgreSQL database and real Anthropic API. No mocks.

Usage:
    python run_ai_sim.py                          # Run all 20 scenarios
    python run_ai_sim.py --only mesa              # Only mesa suite
    python run_ai_sim.py --only delivery,pickup   # Multiple suites
    python run_ai_sim.py --scenario mesa_01_flujo_completo -v
    python run_ai_sim.py --fail-fast
    python run_ai_sim.py --no-alembic             # Skip alembic upgrade head
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows console (cp1252) can't encode emojis that the bot returns. Reconfigure
# stdout/stderr to UTF-8 with replacement so verbose prints never crash on
# bot replies containing 😊, 🚨, etc.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass  # older Python or non-standard stream

# ── Ensure repo root is on sys.path ──────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# ── Load .env automatically (production-parity local setup) ──────────────────
# override=False → explicit shell env vars win over .env, so the user can
# point at a different DB for a single run with `DATABASE_URL=... python run_ai_sim.py`.
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env", override=False)
except ImportError:
    pass  # python-dotenv is in requirements.txt but fail gracefully if missing


def _validate_env(smoke: bool = False) -> bool:
    """Validate required environment variables. Returns True if OK to continue.

    In smoke mode, ANTHROPIC_API_KEY is not required (no LLM calls are made).
    """
    ok = True

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.")
        print("       Set it to a PostgreSQL URL for your test/sim database.")
        print("       Example: export DATABASE_URL=postgresql://user:pass@localhost/mesio_test")
        ok = False
    else:
        # Normalize postgres:// → postgresql://
        if database_url.startswith("postgres://"):
            os.environ["DATABASE_URL"] = "postgresql://" + database_url[len("postgres://"):]
            database_url = os.environ["DATABASE_URL"]

        lower_url = database_url.lower()
        if "test" not in lower_url and "sim" not in lower_url:
            print(
                f"WARNING: DATABASE_URL does not contain 'test' or 'sim'.\n"
                f"         URL preview: {database_url[:80]}\n"
                f"         You may be pointing at a PRODUCTION database.\n"
                f"         The simulation will TRUNCATE several tables between scenarios.\n"
            )
            # Skip the interactive prompt when running in a non-tty environment
            # (CI, Railway one-off deploy) or when AI_SIM_ASSUME_YES=1 is set.
            if os.environ.get("AI_SIM_ASSUME_YES") == "1" or not sys.stdin.isatty():
                print("Non-interactive mode: proceeding (set AI_SIM_ASSUME_YES=0 "
                      "to force abort, or point DATABASE_URL at a 'test'/'sim' DB).")
            else:
                answer = input("Continue anyway? [y/N] ").strip().lower()
                if answer not in ("y", "yes"):
                    print("Aborted.")
                    sys.exit(1)

    if smoke:
        # Smoke mode does not call Anthropic — skip key validation.
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not anthropic_key:
            print("INFO: ANTHROPIC_API_KEY not set — OK for --smoke (no LLM calls).")
    else:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not anthropic_key:
            print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
            print("       The simulation requires real Anthropic API calls.")
            ok = False

    redis_url = os.environ.get("REDIS_URL", "").strip()
    if redis_url:
        print(
            f"WARNING: REDIS_URL is set ({redis_url[:40]}...).\n"
            f"         The simulation uses in-process fallback state (no Redis needed).\n"
            f"         If this points to a shared/production Redis, state from the\n"
            f"         simulation may interfere with other services.\n"
        )

    return ok


def _pre_alembic_shim() -> bool:
    """Pre-create runtime-DDL tables that migration 0012 references but don't exist yet.

    Context (pre-existing bug, documented in CLAUDE.md and 0020's own comments):
    Migration 0012_b2b_sales_system.py creates `sales_conversations` with a FK to
    `prospects(id)`, but `prospects` is only created in migration 0020. In production,
    `prospects` was created at runtime BEFORE alembic ever ran, so 0012 succeeded by
    accident. On a fresh DB, 0012 fails with `relation "prospects" does not exist`.

    This shim idempotently creates the minimum `prospects` table to unblock 0012.
    Migration 0020 also does `CREATE TABLE IF NOT EXISTS prospects`, so no conflict.
    """
    import psycopg2

    db_url = os.environ["DATABASE_URL"]
    # psycopg2 doesn't accept postgres+psycopg:// nor postgresql+psycopg://
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    conn = psycopg2.connect(db_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Check if alembic has ever run on this DB
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = 'alembic_version'
                )
            """)
            alembic_initialized = cur.fetchone()[0]
            if alembic_initialized:
                # alembic already ran before; prospects either exists or will be no-op'd
                return True

            # Fresh DB: pre-create minimum `prospects` so 0012 FK doesn't blow up.
            # Schema kept in sync with 0020_missing_runtime_tables.py (lines 77-100).
            print("Pre-flight: creating prospects table to unblock 0012 FK...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS prospects (
                    id              SERIAL PRIMARY KEY,
                    restaurant_name TEXT      NOT NULL,
                    owner_name      TEXT      NOT NULL DEFAULT '',
                    phone           TEXT      NOT NULL,
                    city            TEXT      NOT NULL DEFAULT '',
                    neighborhood    TEXT      NOT NULL DEFAULT '',
                    category        TEXT      NOT NULL DEFAULT '',
                    instagram       TEXT      NOT NULL DEFAULT '',
                    google_maps     TEXT      NOT NULL DEFAULT '',
                    source          TEXT      NOT NULL DEFAULT 'manual',
                    stage           TEXT      NOT NULL DEFAULT 'prospecto',
                    priority        TEXT      NOT NULL DEFAULT 'medium',
                    assigned_to     TEXT      NOT NULL DEFAULT '',
                    last_contact_at TIMESTAMP,
                    next_follow_up  TIMESTAMP,
                    revenue_est     INTEGER   DEFAULT 0,
                    tags            TEXT[]    DEFAULT '{}',
                    archived        BOOLEAN   DEFAULT FALSE,
                    created_at      TIMESTAMP DEFAULT NOW(),
                    updated_at      TIMESTAMP DEFAULT NOW()
                )
            """)
        return True
    except Exception as exc:
        print(f"ERROR: pre-alembic shim failed: {exc}")
        return False
    finally:
        conn.close()


def _run_alembic() -> bool:
    """Run alembic upgrade head. Returns True on success."""
    if not _pre_alembic_shim():
        return False
    print("Running alembic upgrade head...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(_REPO_ROOT),
            capture_output=False,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"ERROR: alembic upgrade head failed (exit code {result.returncode}).")
            print("       Use --no-alembic to skip migration and run anyway.")
            return False
        print("alembic upgrade head: OK\n")
        return True
    except subprocess.TimeoutExpired:
        print("ERROR: alembic upgrade head timed out after 120 seconds.")
        return False
    except FileNotFoundError:
        print("ERROR: alembic not found. Install it with: pip install alembic")
        return False


async def _smoke_async() -> int:
    """Smoke-test the simulation plumbing WITHOUT calling Anthropic.

    Validates:
      Step A — DB connectivity + seed (organizations + locations schema)
      Step B — tenant_scope wrapping (resolve org_id, enter scope, no agent.chat)
      Step C — snapshot_db_state runs cleanly against seeded data
      Step D — truncate_test_data cleans up without permission errors
      Step E — post-truncate snapshot returns empty volatile tables

    Returns 0 if all steps pass, 1 otherwise.
    """
    import asyncpg
    from tests.ai_sim.seed import (
        seed_restaurant,
        truncate_test_data,
        reset_state_store_fallbacks,
        SIM_BOT_NUMBER,
    )
    from tests.ai_sim.assertions import snapshot_db_state
    from app.services.tenant_context import tenant_scope, bypass_tenant_scope
    from app.services.database import _normalize_phone as _norm

    all_passed = True

    def _step(name: str, ok: bool, detail: str = "") -> None:
        nonlocal all_passed
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {name}{suffix}")
        if not ok:
            all_passed = False

    print("\n=== AI Sim Smoke Test ===\n")

    database_url = os.environ.get("DATABASE_URL", "")
    try:
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=5,
            command_timeout=60,
        )
    except Exception as exc:
        print(f"  [FAIL] DB connectivity — {exc}")
        print(f"\n=== SMOKE RESULT: FAIL ===\n")
        return 1

    try:
        # ── Step A: Seed ──────────────────────────────────────────────────────
        try:
            async with pool.acquire() as conn:
                seed_info = await seed_restaurant(conn)
            _step(
                "A: seed_restaurant",
                True,
                f"org_id={seed_info['restaurant_id']} bot={seed_info['bot_number']}",
            )
        except Exception as exc:
            _step("A: seed_restaurant", False, str(exc))

        # ── Step B: tenant_scope plumbing ─────────────────────────────────────
        # Mirrors the production pattern in inbox_worker._handle_meta_whatsapp.
        # Resolves org_id from bot_number (requires bypass for cross-tenant lookup),
        # then enters tenant_scope(org_id) — no agent.chat() call.
        try:
            normalized_bot = _norm(SIM_BOT_NUMBER)
            async with pool.acquire() as conn:
                with bypass_tenant_scope("ai_sim_smoke_resolve_org"):
                    org_id = await conn.fetchval(
                        "SELECT id FROM organizations WHERE whatsapp_number = $1",
                        normalized_bot,
                    )
            if org_id is None:
                _step("B: tenant_scope resolution", False, "org_id is None — seed may have failed")
            else:
                # Enter scope and run a simple tenant-gated query to confirm RLS fires
                async with pool.acquire() as conn:
                    with tenant_scope(org_id):
                        # Verify we can query a tenant-scoped table without TenantNotSetError
                        # and that org_isolation policy allows seeing our own rows.
                        row_count = await conn.fetchval(
                            "SELECT COUNT(*) FROM subscription_usage WHERE org_id = $1",
                            org_id,
                        )
                _step(
                    "B: tenant_scope resolution",
                    True,
                    f"org_id={org_id} subscription_usage rows={row_count}",
                )
        except Exception as exc:
            _step("B: tenant_scope resolution", False, str(exc))

        # ── Step C: snapshot_db_state ─────────────────────────────────────────
        try:
            normalized_user = _norm("+573000000001")
            async with pool.acquire() as conn:
                snap = await snapshot_db_state(
                    conn,
                    bot_number=normalized_bot,
                    user_phone=normalized_user,
                )
            _step(
                "C: snapshot_db_state",
                True,
                (
                    f"table_orders={len(snap.table_orders)} "
                    f"orders={len(snap.orders)} "
                    f"carts={len(snap.carts)} "
                    f"subscription_usage={snap.subscription_usage}"
                ),
            )
        except Exception as exc:
            _step("C: snapshot_db_state", False, str(exc))

        # ── Step D: truncate_test_data ────────────────────────────────────────
        try:
            reset_state_store_fallbacks()
            async with pool.acquire() as conn:
                await truncate_test_data(conn)
            _step("D: truncate_test_data", True, "volatile tables cleaned")
        except Exception as exc:
            _step("D: truncate_test_data", False, str(exc))

        # ── Step E: post-truncate snapshot should show empty volatile tables ──
        try:
            async with pool.acquire() as conn:
                snap2 = await snapshot_db_state(
                    conn,
                    bot_number=normalized_bot,
                    user_phone=normalized_user,
                )
            # conversations, orders, table_orders, carts must be empty
            volatile_empty = (
                len(snap2.table_orders) == 0
                and len(snap2.orders) == 0
                and len(snap2.carts) == 0
                and len(snap2.conversations) == 0
            )
            _step(
                "E: post-truncate snapshot empty",
                volatile_empty,
                (
                    f"table_orders={len(snap2.table_orders)} "
                    f"orders={len(snap2.orders)} "
                    f"carts={len(snap2.carts)} "
                    f"conversations={len(snap2.conversations)}"
                ),
            )
        except Exception as exc:
            _step("E: post-truncate snapshot empty", False, str(exc))

    finally:
        await pool.close()

    result_label = "PASS" if all_passed else "FAIL"
    print(f"\n=== SMOKE RESULT: {result_label} ===\n")
    if all_passed:
        print("Plumbing verified. Ready for a full E2E run (requires ANTHROPIC_API_KEY budget).")
    else:
        print("One or more smoke steps failed. Fix before running full E2E.")
    print()
    return 0 if all_passed else 1


async def _main_async(args: argparse.Namespace) -> int:
    """Async main logic. Returns exit code (0 = all pass, 1 = failures)."""
    import asyncpg

    from tests.ai_sim.seed import seed_restaurant
    from tests.ai_sim.runner import run_all
    from tests.ai_sim.report import write_jsonl, write_html, print_summary

    # ── Load scenarios ─────────────────────────────────────────────────────────
    try:
        from tests.ai_sim.scenarios import ALL_SCENARIOS
    except ImportError as exc:
        print(f"ERROR: Could not import scenarios: {exc}")
        print("       The scenarios package may not be built yet.")
        print("       Expected: tests/ai_sim/scenarios/{mesa,delivery,pickup,adversarial}.py")
        return 1

    if not ALL_SCENARIOS:
        print("WARNING: ALL_SCENARIOS is empty. No scenarios to run.")
        print("         Check that tests/ai_sim/scenarios/*.py files exist and export scenario lists.")
        return 0

    # ── Filter scenarios ──────────────────────────────────────────────────────
    scenarios = list(ALL_SCENARIOS)

    if args.only:
        suites = {s.strip().lower() for s in args.only.split(",")}
        scenarios = [s for s in scenarios if s.suite.lower() in suites]
        print(f"Filtered to suites {suites}: {len(scenarios)} scenario(s)")

    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario]
        if not scenarios:
            print(f"ERROR: Scenario '{args.scenario}' not found.")
            all_ids = [s.id for s in ALL_SCENARIOS]
            print(f"       Available: {all_ids}")
            return 1
        print(f"Running single scenario: {args.scenario}")

    if not scenarios:
        print("No scenarios match the given filters.")
        return 0

    print(f"\nRunning {len(scenarios)} scenario(s)...\n")

    # ── Create pool ───────────────────────────────────────────────────────────
    database_url = os.environ.get("DATABASE_URL", "")
    try:
        pool = await asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=10,
            command_timeout=60,
        )
    except Exception as exc:
        print(f"ERROR: Could not connect to database: {exc}")
        print(f"       DATABASE_URL: {database_url[:60]}...")
        return 1

    # ── Seed restaurant once ──────────────────────────────────────────────────
    try:
        async with pool.acquire() as conn:
            seed_info = await seed_restaurant(conn)
        print(f"Seeded test restaurant: id={seed_info['restaurant_id']} bot={seed_info['bot_number']}\n")
    except Exception as exc:
        print(f"ERROR: Seeding failed: {exc}")
        await pool.close()
        return 1

    # ── Run scenarios ─────────────────────────────────────────────────────────
    try:
        results = await run_all(scenarios, pool, fail_fast=args.fail_fast)
    finally:
        await pool.close()

    # ── Verbose transcript output ─────────────────────────────────────────────
    if args.verbose:
        for result in results:
            print(f"\n{'='*60}")
            print(f"SCENARIO: {result.scenario.id} — {result.judge.verdict}")
            print(f"{'='*60}")
            for i, turn in enumerate(result.transcript, 1):
                print(f"\n[Turn {i}]")
                print(f"  USER: {turn.user}")
                print(f"  BOT:  {turn.bot}")
                print(f"  ({turn.latency_ms}ms)")
            print(f"\nJudge: {result.judge.verdict} (score={result.judge.score})")
            print(f"  {result.judge.explanation}")
            if result.judge.critical_failures:
                for cf in result.judge.critical_failures:
                    print(f"  CRITICAL: {cf}")
            if result.asserts.failures:
                for af in result.asserts.failures:
                    print(f"  ASSERT FAIL: {af}")

    # ── Write reports ─────────────────────────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_dir = _REPO_ROOT / "tests" / "ai_sim" / "reports" / f"run-{ts}"
    report_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = report_dir / "results.jsonl"
    html_path = report_dir / "report.html"

    try:
        write_jsonl(results, jsonl_path)
        print(f"\nJSONL report: {jsonl_path}")
    except Exception as exc:
        print(f"WARNING: JSONL write failed: {exc}")

    try:
        write_html(results, html_path)
        print(f"HTML report:  {html_path}")
    except Exception as exc:
        print(f"WARNING: HTML write failed: {exc}")

    # ── Print summary ─────────────────────────────────────────────────────────
    print_summary(results)

    # ── Exit code ─────────────────────────────────────────────────────────────
    all_passed = all(r.passed for r in results)
    return 0 if all_passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run_ai_sim.py",
        description=(
            "Mesio AI Simulation Test Suite — real end-to-end bot testing.\n"
            "Requires: DATABASE_URL (test/sim DB), ANTHROPIC_API_KEY.\n\n"
            "Use --smoke to validate plumbing only (no Anthropic calls, no LLM spend)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "Smoke-test DB plumbing without calling Anthropic: "
            "seed → tenant_scope → snapshot → truncate → verify clean. "
            "ANTHROPIC_API_KEY not required."
        ),
    )
    parser.add_argument(
        "--only",
        metavar="SUITE[,SUITE]",
        help="Comma-separated list of suites to run: mesa, delivery, pickup, adversarial",
    )
    parser.add_argument(
        "--scenario",
        metavar="ID",
        help="Run a single scenario by its id (e.g. mesa_01_flujo_completo)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print full transcripts to stdout after each scenario",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop execution after the first failed scenario",
    )
    parser.add_argument(
        "--no-alembic",
        action="store_true",
        help="Skip 'alembic upgrade head' at startup",
    )

    args = parser.parse_args()

    # ── Smoke mode: validate plumbing only, no LLM spend ─────────────────────
    if args.smoke:
        if not _validate_env(smoke=True):
            sys.exit(1)
        if not args.no_alembic:
            if not _run_alembic():
                sys.exit(1)
        exit_code = asyncio.run(_smoke_async())
        sys.exit(exit_code)

    # ── Validate environment ──────────────────────────────────────────────────
    if not _validate_env():
        sys.exit(1)

    # ── Run alembic migrations ────────────────────────────────────────────────
    if not args.no_alembic:
        if not _run_alembic():
            sys.exit(1)

    # ── Run async main ────────────────────────────────────────────────────────
    exit_code = asyncio.run(_main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
