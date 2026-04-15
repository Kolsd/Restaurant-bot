"""
tests/ai_sim/assertions.py — Hard DB assertions for the AI simulation harness.

These check facts that the LLM judge should not have to interpret:
  - row counts
  - dish names in committed orders
  - cart emptiness
  - dead letter count
  - token usage
"""
from __future__ import annotations

import asyncpg

from tests.ai_sim.types import AssertionResult, DBSnapshot, ExpectedState
from app.services.logging import get_logger

log = get_logger(__name__)


def check(expected: ExpectedState, snapshot: DBSnapshot) -> AssertionResult:
    """Compare expected state against the DB snapshot.

    Returns AssertionResult(all_passed, failures).
    """
    failures: list[str] = []

    # ── table_orders count ────────────────────────────────────────────────────
    if expected.table_orders_count is not None:
        actual = len(snapshot.table_orders)
        if actual != expected.table_orders_count:
            failures.append(
                f"table_orders count: expected {expected.table_orders_count}, got {actual}"
            )

    # ── external orders count ─────────────────────────────────────────────────
    if expected.orders_count is not None:
        actual = len(snapshot.orders)
        if actual != expected.orders_count:
            failures.append(
                f"orders count: expected {expected.orders_count}, got {actual}"
            )

    # ── cart emptiness ────────────────────────────────────────────────────────
    if expected.carts_empty_after:
        non_empty = []
        for cart in snapshot.carts:
            items = cart.get("items")
            # items may be a list (asyncpg JSONB → Python list) or a JSON string
            if isinstance(items, str):
                import json
                try:
                    items = json.loads(items)
                except Exception:  # noqa: BLE001
                    items = []
            if items:  # non-empty list
                non_empty.append(cart.get("phone", "?"))
        if non_empty:
            failures.append(
                f"cart not empty after scenario for phone(s): {non_empty}"
            )

    # ── waiter alerts count ───────────────────────────────────────────────────
    if expected.waiter_alerts_count is not None:
        actual = len(snapshot.waiter_alerts)
        if actual != expected.waiter_alerts_count:
            failures.append(
                f"waiter_alerts count: expected {expected.waiter_alerts_count}, got {actual}"
            )

    # ── waiter alert types ────────────────────────────────────────────────────
    if expected.waiter_alerts_types:
        present_types = {a.get("type", a.get("alert_type", "")) for a in snapshot.waiter_alerts}
        for required_type in expected.waiter_alerts_types:
            if required_type not in present_types:
                failures.append(
                    f"waiter_alert type '{required_type}' not found in alerts. "
                    f"Present types: {sorted(present_types)}"
                )

    # ── reservations count ────────────────────────────────────────────────────
    if expected.reservations_count is not None:
        actual = len(snapshot.reservations)
        if actual != expected.reservations_count:
            failures.append(
                f"reservations count: expected {expected.reservations_count}, got {actual}"
            )

    # ── reservation status ────────────────────────────────────────────────────
    if expected.reservations_status is not None and snapshot.reservations:
        # Check the most recent reservation
        latest = sorted(
            snapshot.reservations,
            key=lambda r: r.get("created_at", ""),
            reverse=True,
        )
        actual_status = latest[0].get("status", "") if latest else ""
        if actual_status != expected.reservations_status:
            failures.append(
                f"latest reservation status: expected '{expected.reservations_status}', "
                f"got '{actual_status}'"
            )

    # ── items that must appear in orders ──────────────────────────────────────
    if expected.items_in_orders:
        all_items_text = _collect_all_item_names(snapshot)
        for dish in expected.items_in_orders:
            dish_lower = dish.lower()
            found = any(dish_lower in item_name.lower() for item_name in all_items_text)
            if not found:
                failures.append(
                    f"dish '{dish}' not found in any committed order. "
                    f"Items found: {sorted(all_items_text)[:10]}"
                )

    # ── items that must NOT appear in orders ──────────────────────────────────
    if expected.items_not_in_orders:
        all_items_text = _collect_all_item_names(snapshot)
        for dish in expected.items_not_in_orders:
            dish_lower = dish.lower()
            found = any(dish_lower in item_name.lower() for item_name in all_items_text)
            if found:
                failures.append(
                    f"dish '{dish}' unexpectedly found in committed orders"
                )

    # ── inbox dead letters ────────────────────────────────────────────────────
    actual_dead = snapshot.webhook_inbox_stats.get("dead_letters", 0)
    if actual_dead > expected.inbox_dead_letters:
        failures.append(
            f"webhook_inbox dead letters: expected <= {expected.inbox_dead_letters}, "
            f"got {actual_dead}"
        )

    # ── tokens used ──────────────────────────────────────────────────────────
    if expected.tokens_used_gt_zero:
        tokens = snapshot.subscription_usage.get("total_tokens", 0)
        if tokens <= 0:
            failures.append(
                f"subscription_usage.total_tokens = {tokens} (expected > 0 — LLM may not have run)"
            )

    all_passed = len(failures) == 0
    return AssertionResult(all_passed=all_passed, failures=failures)


def _collect_all_item_names(snapshot: DBSnapshot) -> list[str]:
    """Extract all item name strings from table_orders and external orders."""
    import json

    names: list[str] = []

    def _extract_from_items(items_raw) -> None:
        # Defensive against double-JSON-encoded items (a real bug in the commit
        # path — CLAUDE.md says JSONB should not be json.dumps'd but some code
        # path does it anyway). First decode may give a string; try again.
        for _ in range(2):
            if isinstance(items_raw, str):
                try:
                    items_raw = json.loads(items_raw)
                except Exception:  # noqa: BLE001
                    return
            else:
                break
        if not isinstance(items_raw, list):
            return
        for item in items_raw:
            if isinstance(item, dict):
                name = item.get("name") or item.get("item") or item.get("dish") or ""
                if name:
                    names.append(str(name))

    for row in snapshot.table_orders:
        _extract_from_items(row.get("items"))

    for row in snapshot.orders:
        _extract_from_items(row.get("items"))

    return names


async def snapshot_db_state(
    conn: asyncpg.Connection,
    bot_number: str,
    user_phone: str,
) -> DBSnapshot:
    """Query the DB and return a point-in-time snapshot for assertion/reporting.

    Defensive: uses COALESCE, handles empty results as empty list/dict.
    """
    import json

    def _rows_to_dicts(rows) -> list[dict]:
        if not rows:
            return []
        return [dict(r) for r in rows]

    # ── restaurant_id for this bot ────────────────────────────────────────────
    restaurant_id = await conn.fetchval(
        "SELECT id FROM restaurants WHERE whatsapp_number = $1",
        bot_number,
    )

    # ── table_orders ──────────────────────────────────────────────────────────
    table_orders = _rows_to_dicts(
        await conn.fetch(
            """
            SELECT id, table_id, table_name, phone, items, status, total, created_at
            FROM table_orders
            WHERE phone = $1
            ORDER BY created_at DESC
            """,
            user_phone,
        )
    )

    # ── external orders (delivery / pickup) ───────────────────────────────────
    orders = _rows_to_dicts(
        await conn.fetch(
            """
            SELECT id, phone, items, status, total, order_type, created_at
            FROM orders
            WHERE phone = $1
            ORDER BY created_at DESC
            """,
            user_phone,
        )
    ) if restaurant_id else []

    # ── carts ─────────────────────────────────────────────────────────────────
    # NOTE: carts table uses `cart_data` (not `items`) as the JSONB column name.
    # We alias to 'items' so downstream consumers (assertions, report) see a
    # consistent key across carts/orders/table_orders.
    carts = _rows_to_dicts(
        await conn.fetch(
            """
            SELECT phone, bot_number, cart_data AS items, updated_at
            FROM carts
            WHERE phone = $1 AND bot_number = $2
            """,
            user_phone,
            bot_number,
        )
    )

    # ── waiter_alerts ─────────────────────────────────────────────────────────
    waiter_alerts: list[dict] = []
    if restaurant_id:
        # Alerts are tied to bot_number or to the restaurant's tables
        waiter_alerts = _rows_to_dicts(
            await conn.fetch(
                """
                SELECT id, table_id, table_name, phone, bot_number, alert_type AS type, created_at
                FROM waiter_alerts
                WHERE bot_number = $1 OR phone = $2
                ORDER BY created_at DESC
                """,
                bot_number,
                user_phone,
            )
        )

    # ── reservations ─────────────────────────────────────────────────────────
    reservations: list[dict] = []
    if restaurant_id:
        reservations = _rows_to_dicts(
            await conn.fetch(
                """
                SELECT id, phone, guests, date, time, status, created_at
                FROM reservations
                WHERE restaurant_id = $1
                ORDER BY created_at DESC
                """,
                restaurant_id,
            )
        )

    # ── conversations ─────────────────────────────────────────────────────────
    conversations = _rows_to_dicts(
        await conn.fetch(
            """
            SELECT phone, bot_number, history, updated_at
            FROM conversations
            WHERE phone = $1 AND bot_number = $2
            """,
            user_phone,
            bot_number,
        )
    )

    # ── webhook_inbox stats ───────────────────────────────────────────────────
    inbox_row = await conn.fetchrow(
        """
        SELECT
            COUNT(*)                                          AS total,
            COUNT(*) FILTER (WHERE processed_at IS NULL)     AS pending,
            COUNT(*) FILTER (WHERE last_error LIKE 'DEAD_LETTER:%') AS dead_letters
        FROM webhook_inbox
        """
    )
    webhook_inbox_stats = {
        "total": int(inbox_row["total"]) if inbox_row else 0,
        "pending": int(inbox_row["pending"]) if inbox_row else 0,
        "dead_letters": int(inbox_row["dead_letters"]) if inbox_row else 0,
    }

    # ── subscription_usage ────────────────────────────────────────────────────
    usage_row = None
    if restaurant_id:
        usage_row = await conn.fetchrow(
            """
            SELECT total_tokens, total_invoices
            FROM subscription_usage
            WHERE restaurant_id = $1 AND usage_date = CURRENT_DATE
            """,
            restaurant_id,
        )
    subscription_usage = dict(usage_row) if usage_row else {}

    log.debug(
        "snapshot.taken",
        bot_number=bot_number,
        user_phone=user_phone,
        table_orders=len(table_orders),
        orders=len(orders),
        carts=len(carts),
        waiter_alerts=len(waiter_alerts),
        reservations=len(reservations),
    )

    return DBSnapshot(
        table_orders=table_orders,
        orders=orders,
        carts=carts,
        waiter_alerts=waiter_alerts,
        reservations=reservations,
        conversations=conversations,
        webhook_inbox_stats=webhook_inbox_stats,
        subscription_usage=subscription_usage,
    )
