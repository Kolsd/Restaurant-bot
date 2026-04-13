"""
Load test for concurrent inventory deductions.

Validates that the SELECT FOR UPDATE + WHERE current_stock >= $1 RETURNING
pattern used in _deduct_inventory_in_tx (orders_repo.py) prevents lost
updates under contention — i.e., final stock equals initial_stock minus
the number of successful deductions, with zero overcounting.

The test does NOT invoke commit_order_transaction directly (which also needs
carts, orders, dish_recipes, etc.). Instead it reproduces the exact SQL
pattern from _deduct_inventory_in_tx for the legacy linked_dishes path so
the script is fully self-contained.

Usage:
    DATABASE_URL=postgres://... python scripts/load_test.py
    TEST_DATABASE_URL=postgres://... python scripts/load_test.py

    Options:
        --concurrency N   Number of concurrent order goroutines (default: 50)
        --stock N         Initial current_stock for the test item (default: 100)
        --deduct N        Units to deduct per order (default: 1)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg


# ── Colour helpers (no deps) ────────────────────────────────────────────────

def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


# ── Core concurrent worker ───────────────────────────────────────────────────

async def _place_order(
    pool: asyncpg.Pool,
    inv_id: int,
    deduct_qty: float,
    order_num: int,
    counters: dict,
    latencies: list,
) -> None:
    """
    Mirrors the legacy-path in _deduct_inventory_in_tx:
      1. SELECT … FOR UPDATE  (lock the row)
      2. UPDATE … WHERE current_stock >= $1 RETURNING current_stock
      3. INSERT into inventory_history

    If step 2 returns NULL the item is out of stock → counted as stock_error.
    Any other exception → counted as failure.
    Both abort the transaction (rollback).
    """
    t0 = time.monotonic()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Step 1 — acquire row lock (mirrors FOR UPDATE in escandallo path)
                locked = await conn.fetchrow(
                    """SELECT id, current_stock, min_stock
                       FROM inventory
                       WHERE id = $1
                       FOR UPDATE""",
                    inv_id,
                )
                if locked is None:
                    counters["failures"] += 1
                    return

                available = float(locked["current_stock"])

                # Step 2 — atomic check-and-deduct (exact query from orders_repo.py)
                updated = await conn.fetchrow(
                    """UPDATE inventory
                       SET current_stock = current_stock - $1,
                           updated_at    = NOW()
                       WHERE id = $2
                         AND current_stock >= $1
                       RETURNING current_stock""",
                    deduct_qty, inv_id,
                )

                if updated is None:
                    # Stock was insufficient — this is expected once stock runs out
                    counters["stock_errors"] += 1
                    return

                new_stock = float(updated["current_stock"])

                # Step 3 — history record (mirrors orders_repo.py exactly)
                await conn.execute(
                    """INSERT INTO inventory_history
                       (inventory_id, quantity_delta, stock_after, reason)
                       VALUES ($1, $2, $3, 'load_test')""",
                    inv_id, -deduct_qty, new_stock,
                )

                counters["successes"] += 1

    except Exception as exc:  # noqa: BLE001
        counters["failures"] += 1
        print(f"  [order {order_num}] unexpected error: {type(exc).__name__}: {exc}")
    finally:
        latencies.append(time.monotonic() - t0)


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(concurrency: int, initial_stock: int, deduct_qty: float) -> None:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: Set DATABASE_URL or TEST_DATABASE_URL", file=sys.stderr)
        sys.exit(1)

    pool = await asyncpg.create_pool(
        url,
        min_size=5,
        max_size=concurrency + 5,
        command_timeout=30,
    )
    print(f"Connected. Pool max_size={concurrency + 5}")

    # ── Setup ────────────────────────────────────────────────────────────────
    # Use a unique whatsapp_number so parallel test runs don't collide
    unique_wa = f"+TEST{uuid.uuid4().hex[:12]}"
    rid: int
    inv_id: int

    async with pool.acquire() as conn:
        rid = await conn.fetchval(
            """INSERT INTO restaurants
                   (name, whatsapp_number, features, created_at)
               VALUES ('__load_test__', $1, '{}'::jsonb, NOW())
               RETURNING id""",
            unique_wa,
        )

        inv_id = await conn.fetchval(
            """INSERT INTO inventory
                   (restaurant_id, name, unit, current_stock, min_stock,
                    linked_dishes, cost_per_unit, created_at, updated_at)
               VALUES ($1, 'Load Test Item', 'unidades', $2, 0,
                       '["__LOAD_TEST_DISH__"]'::jsonb, 0, NOW(), NOW())
               RETURNING id""",
            rid, initial_stock,
        )

    print(
        f"Setup: restaurant_id={rid}, inventory_id={inv_id}, "
        f"current_stock={initial_stock}, deduct_per_order={deduct_qty}"
    )
    max_possible = int(initial_stock / deduct_qty)
    print(f"Max possible successes (stock / deduct): {max_possible}")

    # ── Fire concurrent workers ──────────────────────────────────────────────
    counters: dict[str, int] = {"successes": 0, "stock_errors": 0, "failures": 0}
    latencies: list[float] = []

    print(f"\nFiring {concurrency} concurrent goroutines...")
    t_start = time.monotonic()

    await asyncio.gather(
        *[
            _place_order(pool, inv_id, deduct_qty, i, counters, latencies)
            for i in range(concurrency)
        ]
    )

    t_total = time.monotonic() - t_start

    # ── Verify ───────────────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        final_stock = await conn.fetchval(
            "SELECT current_stock FROM inventory WHERE id = $1", inv_id
        )
        history_count = await conn.fetchval(
            "SELECT COUNT(*) FROM inventory_history WHERE inventory_id = $1 AND reason = 'load_test'",
            inv_id,
        )

    successes = counters["successes"]
    stock_errors = counters["stock_errors"]
    failures = counters["failures"]

    expected_stock = float(initial_stock) - (successes * deduct_qty)
    stock_correct = abs(float(final_stock) - expected_stock) < 0.001
    history_matches = history_count == successes

    # ── Report ───────────────────────────────────────────────────────────────
    sep = "=" * 62
    print(f"\n{sep}")
    print(_bold("LOAD TEST RESULTS"))
    print(sep)
    print(f"{'Concurrency:':<28}{concurrency}")
    print(f"{'Initial stock:':<28}{initial_stock}")
    print(f"{'Deduct per order:':<28}{deduct_qty}")
    print(f"{'Successes:':<28}{successes}")
    print(f"{'Stock errors (expected):':<28}{stock_errors}")
    print(f"{'Unexpected failures:':<28}{failures}")
    print(f"{'Final stock:':<28}{float(final_stock)}")
    print(f"{'Expected stock:':<28}{expected_stock}")
    print(f"{'History rows inserted:':<28}{history_count}  (should match successes)")
    print(sep)

    passed = stock_correct and history_matches and failures == 0

    stock_label = _green("YES") if stock_correct else _red("NO — LOST UPDATES DETECTED!")
    hist_label  = _green("YES") if history_matches else _red(f"NO — got {history_count}, want {successes}")
    fail_label  = _green("OK") if failures == 0 else _red(f"{failures} unexpected errors")

    print(f"{'Stock consistent:':<28}{stock_label}")
    print(f"{'History consistent:':<28}{hist_label}")
    print(f"{'No unexpected failures:':<28}{fail_label}")
    print(sep)

    print(f"{'Total wall time:':<28}{t_total:.3f}s")
    if latencies:
        latencies.sort()
        n = len(latencies)
        avg  = sum(latencies) / n
        p50  = latencies[n // 2]
        p95  = latencies[int(n * 0.95)]
        p99  = latencies[min(int(n * 0.99), n - 1)]
        print(f"{'Latency avg:':<28}{avg*1000:.1f}ms")
        print(f"{'Latency p50:':<28}{p50*1000:.1f}ms")
        print(f"{'Latency p95:':<28}{p95*1000:.1f}ms")
        print(f"{'Latency p99:':<28}{p99*1000:.1f}ms")
    print(sep)

    # ── Cleanup ──────────────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM inventory_history WHERE inventory_id = $1 AND reason = 'load_test'",
            inv_id,
        )
        await conn.execute("DELETE FROM inventory WHERE id = $1", inv_id)
        await conn.execute("DELETE FROM restaurants WHERE id = $1", rid)

    print("Cleanup complete.")
    await pool.close()

    if not passed:
        print(_red("\nFAILED: Consistency violation detected (see above)."))
        sys.exit(1)
    else:
        print(_green("\nPASSED: No lost updates. Inventory is consistent under contention."))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--concurrency", type=int, default=50,
        help="Number of concurrent goroutines (default: 50)",
    )
    parser.add_argument(
        "--stock", type=int, default=100,
        help="Initial current_stock for the test item (default: 100)",
    )
    parser.add_argument(
        "--deduct", type=float, default=1.0,
        help="Units to deduct per order (default: 1.0)",
    )
    args = parser.parse_args()

    asyncio.run(main(args.concurrency, args.stock, args.deduct))
