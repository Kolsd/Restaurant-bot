"""
Tables / POS repository — Fase 6 extraction from app.services.database.
Migrated to tenant_connection() — RLS pilot Step 3.

Covers the Tables / POS aggregate:
  - restaurant_tables (CRUD: init DDL, get, create, auto-create, delete, get by id)
  - table_orders (save, status, merge items, list, get bill, close, mark factura,
                  get first order, active order, cleanup, open session by phone,
                  has pending invoice, order ticket data for split checks)
  - waiter_alerts (init DDL, create, list, dismiss)
  - table_sessions (init DDL, get active, create, touch, mark order/delivered/nps,
                    close, reopen, stale/closeable helpers, closed history)
  - table_checks / split checks (init DDL, create, get, finalize payment, delete open,
                                 attach proposal/proof, set tip, list proposals, ticket)

Call sites that import via `app.services.database` continue to work through the
re-export shim added to that module.

Bypass rationale:
  - Scheduler functions (db_get_stale_sessions, db_get_closeable_sessions,
    db_get_active_session_table_ids) iterate across ALL tenants by design.
  - Kitchen/delivery views (db_get_delivery_orders_for_cashier) are
    tenant-scoped via active tenant_scope() at the call site.
  - db_get_waiter_alerts(org_id) is tenant-scoped; call site must pass org_id.
  - db_verify_branch_is_child queries `restaurants` across tenant boundary.
"""

from __future__ import annotations

import json

from app.services.money import to_decimal, ZERO
from app.services.logging import get_logger
from app.services.tenant_db import tenant_connection
from app.services.tenant_context import bypass_tenant_scope

log = get_logger(__name__)


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── restaurant_tables ────────────────────────────────────────────────────────
# Schema managed by Alembic — do NOT add DDL here.
# restaurant_tables, table_orders             → 0001_initial_schema.py
# capacity, table_type, zone, position_x/y   → 0014_reservation_tables_v2.py
# base_order_id, sub_number, station cols    → 0001_initial_schema.py
# idx_table_orders_base, idx_table_orders_station → 0001_initial_schema.py

async def db_init_tables():
    """No-op: schema handled by Alembic (run `alembic upgrade head` before deploying)."""
    pass


async def db_get_tables(branch_id: int = None):
    """
    Devuelve las mesas.
    Si branch_id tiene un número, trae las de esa sede.
    Si branch_id es None, trae TODAS las mesas (admin global).

    Wave-2: filter is by `location_id` (canonical sede id) — not the legacy
    `branch_id` column. Pre-Wave-2 data sometimes has branch_id stuck on the
    org_id (e.g. restaurant-tables for org 8 stored with branch_id=8 but
    location_id=1). The endpoint resolves location_id from the user's
    restaurant context post-Wave-2; using branch_id here would fail to find
    tables whose legacy branch_id wasn't backfilled. Param name preserved
    for backward-compat at the function signature; semantically it's a
    location_id.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND location_id=$1 ORDER BY number", branch_id)
        else:
            # Modo Admin Global: Trae todo
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")

        return [_serialize(dict(r)) for r in rows]


async def db_create_table(table_id: str, number: int, name: str, branch_id: int = None,
                          capacity: int = 4, table_type: str = "interior", zone: str = ""):
    """# Requires active tenant_scope() or bypass_tenant_scope().

    Wave-2: restaurant_tables has org_id NOT NULL + location_id NOT NULL
    (location_id is NOT in 0037d's _RELAX_TABLES list — restaurant_tables
    is admin-config, always inserted with explicit sede context). The
    INSERT must populate both: org_id from the GUC, location_id from
    branch_id (they describe the same sede — branch_id is the legacy
    column name, location_id is the canonical Wave-2 column).
    """
    async with tenant_connection() as conn:
        await conn.execute("""
            INSERT INTO restaurant_tables
                (id, number, name, branch_id, location_id, org_id, active, capacity, table_type, zone)
            VALUES ($1, $2, $3, $4, $4,
                    NULLIF(current_setting('app.org_id', true), '')::bigint,
                    TRUE, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name,
                branch_id=EXCLUDED.branch_id, location_id=EXCLUDED.location_id, active=TRUE,
                capacity=EXCLUDED.capacity, table_type=EXCLUDED.table_type, zone=EXCLUDED.zone
        """, table_id, number, name, branch_id, capacity, table_type, zone)


async def db_auto_create_table(restaurant_id: int) -> dict:
    """
    Automatically creates a table by finding the first available number.
    The name is the plain number ("1", "2"); the id is table-{restaurant_id}-{number}.
    Automatically reuses numbers from tables that have been deleted.

    Wave-2: `restaurant_id` here is the location_id of the branch where
    the table is created (not the org_id). Legacy naming preserved for
    backward-compat with the call site, but the semantics are branch-level.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    # branch_id is always the location id of the sede this table belongs to.
    branch_id = restaurant_id

    async with tenant_connection() as conn:
        # 1. Get all numbers currently in use (ignoring deleted ones)
        rows = await conn.fetch("SELECT number FROM restaurant_tables WHERE branch_id=$1 AND active=TRUE", branch_id)

        used_numbers = {r["number"] for r in rows}

        # 2. Find the first available "gap" (if table 2 was deleted, the next one will be 2)
        new_number = 1
        while new_number in used_numbers:
            new_number += 1

        # 3. The name diners and staff see is just the number ("Mesa 3"); the
        #    sede only goes into the id, which must be unique across sedes.
        table_name = str(new_number)
        table_id = f"table-{restaurant_id}-{new_number}"

        # 4. Insert or reactivate if the ID already existed in the database
        # Wave-2: same shape as db_create_table — must populate org_id (from GUC)
        # and location_id (mirror of branch_id) explicitly. See db_create_table.
        # branch_id column is INTEGER; location_id is BIGINT.
        # Use separate parameters with explicit casts to avoid asyncpg AmbiguousParameterError
        # when $4 appears in both an INTEGER and a BIGINT context.
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active)
            VALUES ($1, $2, $3, $4::integer, $5::bigint,
                    NULLIF(current_setting('app.org_id', true), '')::bigint,
                    TRUE)
            ON CONFLICT (id) DO UPDATE SET active=TRUE, name=EXCLUDED.name, number=EXCLUDED.number
        """, table_id, new_number, table_name, branch_id, branch_id)

        return {
            "id": table_id,
            "number": new_number,
            "name": table_name,
            "branch_id": branch_id
        }


async def db_delete_table(table_id: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE restaurant_tables SET active=FALSE WHERE id=$1", table_id)


async def db_get_table_by_id(table_id: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurant_tables WHERE id=$1", table_id)
        return _serialize(dict(row)) if row else None


# ── table_orders ─────────────────────────────────────────────────────────────

async def db_save_table_order(order: dict):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        # org_id is NOT NULL (FK into organizations). Post-Wave-2 we NEVER
        # fall back to branch_id — it's a location_id now, not a tenant key.
        # Resolve from restaurant_tables.org_id if the caller didn't provide.
        rid = order.get('org_id') or order.get('restaurant_id')
        if rid is None:
            rid = await conn.fetchval(
                "SELECT org_id FROM restaurant_tables WHERE id=$1",
                order['table_id'],
            )
            if rid is None:
                raise ValueError(
                    f"Cannot resolve org_id for table_order {order['id']} "
                    f"(table_id={order['table_id']})"
                )
        row = await conn.fetchrow("""
            INSERT INTO table_orders
                (id, table_id, table_name, phone, items, status, notes, total,
                 base_order_id, sub_number, station, branch_id, org_id,
                 channel, waiter_staff_id, pending_table_validation, location_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::integer,$13,$14,$15,$16,$17::bigint)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items,
                status=EXCLUDED.status,
                notes=EXCLUDED.notes,
                total=EXCLUDED.total,
                branch_id=EXCLUDED.branch_id,
                location_id=EXCLUDED.location_id,
                updated_at=NOW()
            RETURNING id, table_id, org_id, branch_id, (xmax = 0) AS inserted
        """, order['id'], order['table_id'], order['table_name'], order['phone'],
            # The pool's jsonb codec serializes; json.dumps here stored a
            # JSON *string* instead of an array (found 2026-09-25).
            order['items'],
            order.get('status', 'recibido'),
            order.get('notes', ''),
            order.get('total', 0),
            order.get('base_order_id'),
            order.get('sub_number', 1),
            order.get('station', 'all'),
            order.get('branch_id'),
            rid,
            order.get('channel'),
            order.get('waiter_staff_id'),
            bool(order.get('pending_table_validation', False)),
            # branch_id IS the sede id (post-0057). Every sede-filtered read
            # (kitchen, bar, waiter) filters location_id, and no trigger fills
            # it, so without this a sede never saw its own table orders.
            order.get('branch_id'))

    # Publish OUTSIDE the transaction block (tenant_connection() has already
    # committed and released the connection by here) — table_order.created on
    # first insert, table_order.updated on every subsequent save/merge of the
    # same id (ON CONFLICT DO UPDATE, e.g. status/items refresh).
    from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
    topic = "table_order.created" if row["inserted"] else "table_order.updated"
    await realtime.publish(
        row["org_id"], topic,
        location_id=row["branch_id"], table_id=row["table_id"], entity_id=row["id"],
    )


async def db_get_base_order_status(base_order_id: str) -> str | None:
    """Returns the status of the base order record itself (not sub-orders).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM table_orders WHERE id=$1", base_order_id
        )
        return row["status"] if row else None


async def db_get_table_orders(status: str = None):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        if status:
            rows = await conn.fetch("SELECT * FROM table_orders WHERE status=$1 ORDER BY created_at ASC", status)
        else:
            rows = await conn.fetch("SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') ORDER BY created_at ASC")
        result = []
        for r in rows:
            d = _serialize(dict(r))
            if isinstance(d['items'], str): d['items'] = json.loads(d['items'])
            result.append(d)
        return result


async def db_update_table_order_status(order_id: str, status: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1 "
            "RETURNING id, table_id, org_id, branch_id",
            order_id, status,
        )

    if row is not None:
        from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
        await realtime.publish(
            row["org_id"], "table_order.updated",
            location_id=row["branch_id"], table_id=row["table_id"], entity_id=row["id"],
        )


async def db_get_base_order_id(table_id: str) -> str | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        # FIX: Only return a base_order_id when there's an active session for this table.
        # Without this check, a new customer at a table with leftover orders from a
        # previous session would get labeled "Adicional #N" instead of starting fresh.
        session_row = await conn.fetchrow(
            "SELECT id FROM table_sessions WHERE table_id=$1 AND status='active' LIMIT 1",
            table_id,
        )
        if not session_row:
            return None
        row = await conn.fetchrow("""
            SELECT COALESCE(base_order_id, id) as base_id
            FROM table_orders
            WHERE table_id=$1 AND status NOT IN ('factura_entregada', 'cancelado')
            ORDER BY created_at ASC LIMIT 1
        """, table_id)
        return row['base_id'] if row else None


async def db_get_latest_base_order_id_for_table(table_id: str) -> str | None:
    """Like db_get_base_order_id, but for READ-ONLY status views, not for
    deciding whether a NEW round should attach to an existing group.

    Two differences from db_get_base_order_id, both deliberate:
      - Does NOT require an active table_sessions row — a diner polling
        GET /api/diner/status right after their check is paid (which may
        close out the whole table) must still be able to resolve the group
        they just paid into.
      - Does NOT exclude status='factura_entregada' — that status is
        exactly the "already fully paid" state a payment-status view needs
        to report, not hide. db_get_base_order_id excludes it because it
        answers a different question ("what group should a NEW order round
        attach to"), which is why this is a SEPARATE function rather than a
        parameter on that one — changing that one's filter would change
        order-grouping behaviour for the WhatsApp/diner order-send path,
        governed by CLAUDE.md "Reglas del Bot — NO ROMPER".

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(base_order_id, id) as base_id
            FROM table_orders
            WHERE table_id=$1 AND status != 'cancelado'
            ORDER BY created_at DESC LIMIT 1
        """, table_id)
        return row['base_id'] if row else None


async def db_get_next_sub_number(base_order_id: str) -> int:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT MAX(sub_number) as max_sub FROM table_orders WHERE base_order_id=$1 OR id=$1", base_order_id)
        return (row['max_sub'] or 0) + 1


async def db_get_table_bill(base_order_id: str) -> dict:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        rows = await conn.fetch("SELECT * FROM table_orders WHERE base_order_id=$1 OR id=$1 ORDER BY created_at ASC", base_order_id)
        if not rows: return {}
        sub_orders = []
        total = 0
        for r in rows:
            d = _serialize(dict(r))
            if isinstance(d['items'], str): d['items'] = json.loads(d['items'])
            sub_orders.append(d)
            total += d.get('total', 0)
        first = sub_orders[0]
        return {
            "base_order_id": base_order_id, "table_name": first.get('table_name', ''),
            "phone": first.get('phone', ''), "sub_orders": sub_orders, "total": total,
        }


async def db_close_table_bill(base_order_id: str) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        result = await conn.execute("UPDATE table_orders SET status='factura_entregada', updated_at=NOW() WHERE (base_order_id=$1 OR id=$1) AND status NOT IN ('cancelado')", base_order_id)
        return result != "UPDATE 0"


async def db_mark_invoice_generated(base_order_id: str) -> None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE table_orders SET status='factura_generada', updated_at=NOW() "
            "WHERE (id=$1 OR base_order_id=$1) AND status NOT IN ('cancelado','factura_entregada')",
            base_order_id
        )


async def db_get_first_table_order(base_order_id: str) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT phone, table_id, status FROM table_orders "
            "WHERE (id=$1 OR base_order_id=$1) ORDER BY created_at ASC LIMIT 1",
            base_order_id
        )
    return _serialize(dict(row)) if row else None


async def db_cleanup_after_checkout(phone: str) -> None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)
        await conn.execute("DELETE FROM carts WHERE phone=$1", phone)


async def db_get_open_session_by_phone(phone: str) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL ORDER BY started_at DESC LIMIT 1",
            phone
        )
    return _serialize(dict(row)) if row else None


async def db_has_pending_invoice(phone: str) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT id FROM table_orders WHERE phone=$1 AND status='entregado' LIMIT 1", phone)
        return row is not None


async def db_get_active_table_order(phone: str, table_id: str) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM table_orders
            WHERE phone=$1 AND table_id=$2
              AND status NOT IN ('factura_entregada','cancelado')
            ORDER BY created_at DESC LIMIT 1
        """, phone, table_id)
        if not row:
            return None
        d = _serialize(dict(row))
        if isinstance(d['items'], str):
            d['items'] = json.loads(d['items'])
        return d


# ── Split Checks / Ticket data ────────────────────────────────────────────────

async def db_get_order_ticket_data(base_order_id: str, branch_id: int = None) -> dict | None:
    """
    Returns the aggregated items and total across all sub-orders of a ticket.
    Used by create_checks to validate quantities before creating the split.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            rows = await conn.fetch(
                # branch_id-guard-allow: location_id passed by caller; table_orders.branch_id == location_id post-0057
                """SELECT * FROM table_orders
                   WHERE (id = $1 OR base_order_id = $1) AND branch_id = $2
                   ORDER BY created_at ASC""",
                base_order_id, branch_id
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM table_orders
                   WHERE id = $1 OR base_order_id = $1
                   ORDER BY created_at ASC""",
                base_order_id
            )
    if not rows:
        return None
    all_items = []
    total = ZERO
    first = dict(rows[0])
    for row in rows:
        d = dict(row)
        items = d.get("items", [])
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        if isinstance(items, list):
            all_items.extend(items)
        total += to_decimal(d.get("total") or 0)
    return {
        "base_order_id": base_order_id,
        "table_name": first.get("table_name", ""),
        "items": all_items,
        "total": float(total),  # JSON boundary
        "org_id": first.get("org_id"),
    }


# ── waiter_alerts ─────────────────────────────────────────────────────────────

async def db_init_waiter_alerts():
    """No-op: waiter_alerts managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_create_waiter_alert(
    phone: str, org_id: int, alert_type: str, message: str,
    table_id: str = "", table_name: str = "", location_id: int | None = None,
) -> dict:
    """
    Create a waiter alert. For non-billing alerts (alert_type='waiter'), if an
    open alert already exists for the same table within the last 60 seconds,
    append the new message to the existing alert instead of creating a duplicate.
    This prevents the waiter from receiving multiple pings when the customer
    mentions the same request across two consecutive turns.

    location_id: the specific sede (branch) this alert belongs to, when known
    by the caller. Optional and additive — many call sites predate Location
    tracking and still pass None, which is why db_get_waiter_alerts also
    matches legacy NULL-location rows (see that function's docstring).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load

    topic = "waiter_alert.created"
    async with tenant_connection() as conn:
        # Dedup window: 60 seconds for waiter (non-bill) alerts only
        if alert_type == "waiter" and table_id:
            existing = await conn.fetchrow(
                """
                SELECT id, message FROM waiter_alerts
                WHERE table_id = $1
                  AND org_id = $2
                  AND alert_type = 'waiter'
                  AND dismissed = FALSE
                  AND created_at > NOW() - INTERVAL '60 seconds'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                table_id, org_id,
            )
            if existing:
                combined = f"{existing['message']} | {message}"
                row = await conn.fetchrow(
                    "UPDATE waiter_alerts SET message = $1 WHERE id = $2 RETURNING *",
                    combined, existing["id"],
                )
                topic = "waiter_alert.updated"

        if topic == "waiter_alert.created":
            row = await conn.fetchrow(
                "INSERT INTO waiter_alerts (table_id, table_name, phone, org_id, alert_type, message, location_id) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
                table_id, table_name, phone, org_id, alert_type, message, location_id,
            )
        result = _serialize(dict(row))

    # Publish OUTSIDE the transaction block (tenant_connection() has already
    # committed by here) — .created for a genuinely new alert, .updated when
    # an existing open alert absorbed this message via the 60s dedup window.
    await realtime.publish(
        row["org_id"], topic,
        location_id=row["location_id"], table_id=row["table_id"], entity_id=str(row["id"]),
    )
    return result


async def db_get_waiter_alerts(org_id: int, location_id: int | None = None) -> list:
    """List active (non-dismissed, <2h old) waiter alerts for an org.

    location_id: when given, restricts to alerts for THAT sede plus legacy
    rows with location_id IS NULL (created before this column was populated —
    they must not vanish for tenants that had alerts before this change).
    When omitted (None), no location filter is applied — every location
    of this org is returned (today's behaviour, e.g. an owner
    viewing "all sedes").

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if location_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM waiter_alerts WHERE org_id=$1 AND dismissed=FALSE "
                "AND created_at > NOW() - INTERVAL '2 hours' "
                "AND (location_id = $2 OR location_id IS NULL) "
                "ORDER BY created_at DESC",
                org_id, location_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM waiter_alerts WHERE org_id=$1 AND dismissed=FALSE "
                "AND created_at > NOW() - INTERVAL '2 hours' ORDER BY created_at DESC",
                org_id,
            )
        return [_serialize(dict(r)) for r in rows]


async def db_dismiss_waiter_alert(alert_id: int) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        result = await conn.execute("UPDATE waiter_alerts SET dismissed=TRUE WHERE id=$1", alert_id)
        dismissed = result == "UPDATE 1"
        ctx = None
        if dismissed:
            # Best-effort context read for the SSE publish below — kept as a
            # SEPARATE statement (not folded into the UPDATE ... RETURNING)
            # so the core dismiss/not-found determination above stays exactly
            # the pre-existing `result == "UPDATE 1"` contract.
            try:
                ctx = await conn.fetchrow(
                    "SELECT table_id, org_id, location_id FROM waiter_alerts WHERE id=$1",
                    alert_id,
                )
            except Exception:
                ctx = None

    if dismissed and ctx is not None:
        try:
            from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
            await realtime.publish(
                ctx["org_id"], "waiter_alert.updated",
                location_id=ctx["location_id"], table_id=ctx["table_id"], entity_id=str(alert_id),
            )
        except Exception:
            log.warning("realtime.waiter_alert_dismissed.publish_failed", alert_id=alert_id, exc_info=True)
    return dismissed


# ── table_sessions ────────────────────────────────────────────────────────────

async def db_init_table_sessions():
    """No-op: table_sessions managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_get_active_session(phone: str, org_id: int) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND org_id=$2 AND status='active' ORDER BY started_at DESC LIMIT 1", phone, org_id)
        return _serialize(dict(row)) if row else None


async def db_get_active_session_on_table_by_other_phone(table_id: str, phone: str) -> dict | None:
    """Return an active session for `table_id` held by a phone OTHER than `phone`.

    Rule #5 table cooldown: used to reject a second customer scanning the same
    QR while the first customer's session is still active.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_sessions WHERE table_id=$1 AND phone<>$2 AND status='active' ORDER BY started_at DESC LIMIT 1",
            table_id,
            phone,
        )
        return _serialize(dict(row)) if row else None


async def db_get_active_session_by_table_id(table_id: str) -> dict | None:
    """Return the active session for a table_id (phone, org_id).

    Used by pre-cuenta to identify the customer to message.
    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """SELECT phone, org_id
               FROM table_sessions
               WHERE table_id = $1 AND status = 'active'
               ORDER BY started_at DESC LIMIT 1""",
            table_id,
        )
    return _serialize(dict(row)) if row else None


async def db_get_session_join_code(table_id: str, org_id: int) -> str | None:
    """Return the join_code of the active session for this table, or None if
    the table has no active session (caller becomes the host).

    Used by detect_table_context Capa 2: if a session already exists on the
    table, the bot must ask the new participant for the code before opening
    a second session.

    # Requires active tenant_scope() or bypass_tenant_scope_if_unset().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT join_code
            FROM table_sessions
            WHERE table_id = $1
              AND org_id = $2
              AND status = 'active'
            ORDER BY started_at DESC
            LIMIT 1
            """,
            table_id, org_id,
        )
        if row is None:
            return None
        return row["join_code"]  # may still be None if host didn't set it yet


async def db_set_session_join_code(session_id: int, join_code: str) -> bool:
    """Atomically set join_code on a session that does NOT have one yet.

    Returns True if the UPDATE touched exactly one row (success).
    Returns False if the session already had a code (race between two
    concurrent hosts opening the same mesa simultaneously — rare).

    Uses WHERE join_code IS NULL so the operation is idempotent/safe.

    # Requires active tenant_scope() or bypass_tenant_scope_if_unset().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            UPDATE table_sessions
            SET join_code = $2
            WHERE id = $1
              AND join_code IS NULL
            RETURNING id
            """,
            session_id, join_code,
        )
        return row is not None


async def db_link_participant_session(
    phone: str,
    org_id: int,
    table_id: str,
    table_name: str,
    join_code: str,
    location_id: int | None,
) -> dict | None:
    """Open a NEW table_session for a participant joining an existing mesa
    with the correct join_code. Verifies the code against the active host
    session for that table.

    Returns the new session dict if the code matched and the session was
    created. Returns None if no active session with that join_code exists
    for the table (wrong code or stale).

    Steps:
      1. SELECT the active session WHERE table_id=$1 AND join_code=$2 —
         if no row, the code is wrong → return None.
      2. INSERT a new table_sessions row for phone+bot+table_id with the
         SAME join_code so all participants share the identifier.
         Auto-assigns a mesero using the same load-balanced logic as
         db_create_table_session.

    # Requires active tenant_scope() or bypass_tenant_scope_if_unset().
    """
    async with tenant_connection() as conn:
        # Step 1: verify the code matches an active session for this table.
        host_row = await conn.fetchrow(
            """
            SELECT id, assigned_staff_id
            FROM table_sessions
            WHERE table_id = $1
              AND join_code = $2
              AND status = 'active'
            LIMIT 1
            """,
            table_id, join_code,
        )
        if host_row is None:
            return None  # wrong code or no active session

        # Step 2: auto-assign a mesero (load-balanced, same as db_create_table_session).
        assigned_staff_id = None
        if location_id is not None:
            assigned_staff_id = await conn.fetchval(
                """
                SELECT s.id
                FROM staff s
                WHERE s.location_id = $1
                  AND s.active = true
                  AND (s.role = 'mesero' OR s.roles @> '"mesero"'::jsonb)
                ORDER BY (
                    SELECT COUNT(*)
                    FROM table_sessions ts
                    WHERE ts.assigned_staff_id = s.id
                      AND ts.status = 'active'
                ) ASC,
                s.id ASC
                LIMIT 1
                """,
                location_id,
            )

        # Step 3: insert the new participant session with the same join_code.
        row = await conn.fetchrow(
            """
            INSERT INTO table_sessions
                (phone, table_id, table_name,
                 org_id, location_id, assigned_staff_id,
                 join_code, status, last_activity)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'active', NOW())
            RETURNING *
            """,
            phone, table_id, table_name,
            org_id, location_id, assigned_staff_id,
            join_code,
        )
        session = _serialize(dict(row))
        log.info(
            "table_session.participant_joined",
            session_id=session.get("id"),
            table_id=table_id,
            join_code=join_code,
        )
        return session


async def db_create_table_session(
    phone: str,
    org_id: int,
    table_id: str,
    table_name: str,
    location_id: int | None = None,
    assigned_staff_id: str | None = None,
) -> dict:
    """# Requires active tenant_scope() or bypass_tenant_scope().

    location_id is resolved from restaurant_tables when not passed.
    assigned_staff_id: if not explicitly provided, auto-assigned to the
    least-loaded mesero at the location (fewest active table_sessions).
    If no mesero exists at the location, remains NULL — no exception raised.
    """
    async with tenant_connection() as conn:
        if location_id is None:
            location_id = await conn.fetchval(
                "SELECT location_id FROM restaurant_tables WHERE id=$1 AND org_id=$2",
                table_id, org_id,
            )

        # Auto-assign least-loaded mesero when no explicit assignment is given.
        if assigned_staff_id is None and location_id is not None:
            assigned_staff_id = await conn.fetchval(
                """
                SELECT s.id
                FROM staff s
                WHERE s.location_id = $1
                  AND s.active = true
                  AND (s.role = 'mesero' OR s.roles @> '"mesero"'::jsonb)
                ORDER BY (
                    SELECT COUNT(*)
                    FROM table_sessions ts
                    WHERE ts.assigned_staff_id = s.id
                      AND ts.status = 'active'
                ) ASC,
                s.id ASC
                LIMIT 1
                """,
                location_id,
            )

        row = await conn.fetchrow(
            "INSERT INTO table_sessions "
            "(phone, table_id, table_name, org_id, location_id, "
            "assigned_staff_id, status, last_activity) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW()) RETURNING *",
            phone, table_id, table_name, org_id, location_id,
            assigned_staff_id,
        )
        session = _serialize(dict(row))
        if assigned_staff_id is not None:
            log.info(
                "table_session.mesero_assigned",
                session_id=session.get("id"),
                staff_id=str(assigned_staff_id),
                location_id=location_id,
            )
        else:
            log.warning(
                "table_session.no_mesero_available",
                location_id=location_id,
                reason="no active mesero at location",
            )
        return session


async def db_touch_session(phone: str, org_id: int):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW() WHERE phone=$1 AND org_id=$2 AND status='active'", phone, org_id)


async def db_session_mark_order(phone: str, org_id: int):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET has_order=TRUE, last_activity=NOW() WHERE phone=$1 AND org_id=$2 AND status='active'", phone, org_id)


async def db_session_mark_delivered(phone: str, org_id: int, total: int = 0):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET order_delivered=TRUE, last_activity=NOW(), total_spent=$3 WHERE phone=$1 AND org_id=$2 AND status='active'", phone, org_id, total)


async def db_mark_session_nps_pending(phone: str, org_id: int) -> None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE table_sessions SET status='nps_pending', closed_by='factura_entregada', last_activity=NOW() "
            "WHERE phone=$1 AND org_id=$2 AND status='active' "
            "RETURNING id, table_id, org_id, location_id",
            phone, org_id
        )

    if row is not None:
        from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
        # entity_id is the table_sessions PK (an int, stringified) — NEVER the
        # phone: on the WhatsApp channel `phone` is a real phone number, and
        # the contract forbids personal data in events (ids/topics only).
        await realtime.publish(
            row["org_id"], "nps.updated",
            location_id=row["location_id"], table_id=row["table_id"] or None,
            entity_id=str(row["id"]),
        )


async def db_close_session(phone: str, org_id: int, reason: str = "manual", closed_by_username: str = "") -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("""
            UPDATE table_sessions
            SET status='closed', closed_at=NOW(), closed_by=$3, closed_by_username=$4,
                summary=jsonb_build_object('close_reason',$3::text,'closed_by_user',$4::text)
            WHERE phone=$1 AND org_id=$2 AND status IN ('active','nps_pending') RETURNING *
        """, phone, org_id, reason, closed_by_username)
        return _serialize(dict(row)) if row else None


async def db_mark_session_warned(session_id: int) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        # The AND inactivity_warned=FALSE ensures only 1 worker can do the UPDATE
        result = await conn.execute(
            "UPDATE table_sessions SET inactivity_warned=TRUE WHERE id=$1 AND inactivity_warned=FALSE",
            session_id
        )
        return result == "UPDATE 1"


async def db_get_stale_sessions() -> list:
    """
    Cross-tenant scheduler function — iterates ALL active sessions.

    # Uses bypass_tenant_scope internally (scheduler admin operation).
    """
    with bypass_tenant_scope("scheduler: stale session sweep across all tenants"):
        async with tenant_connection() as conn:
            rows = await conn.fetch("""
                SELECT * FROM table_sessions WHERE status='active' AND inactivity_warned=FALSE
                AND ((has_order=FALSE AND last_activity < NOW() - INTERVAL '10 minutes')
                  OR (order_delivered=TRUE AND last_activity < NOW() - INTERVAL '60 minutes'))
            """)
            return [_serialize(dict(r)) for r in rows]


async def db_get_closeable_sessions() -> list:
    """
    Cross-tenant scheduler function — iterates ALL sessions needing closure.

    # Uses bypass_tenant_scope internally (scheduler admin operation).
    """
    with bypass_tenant_scope("scheduler: closeable session sweep across all tenants"):
        async with tenant_connection() as conn:
            rows = await conn.fetch("""
                SELECT * FROM table_sessions WHERE
                (status='active' AND inactivity_warned=TRUE AND last_activity < NOW() - INTERVAL '5 minutes')
                OR (status='nps_pending' AND last_activity < NOW() - INTERVAL '5 minutes')
            """)
            return [_serialize(dict(r)) for r in rows]


async def db_get_session_by_id(session_id: int) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1", session_id)
        return _serialize(dict(row)) if row else None


async def db_reopen_session(session_id: int) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        target = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1 AND status='closed'", session_id)
        if not target: return None
        phone = target["phone"]
        org_id = target["org_id"]
        await conn.execute("UPDATE table_sessions SET status='closed', closed_at=NOW(), closed_by='superseded', closed_by_username='' WHERE phone=$1 AND org_id=$2 AND status='active'", phone, org_id)
        row = await conn.fetchrow("UPDATE table_sessions SET status='active', closed_at=NULL, closed_by='', closed_by_username='', inactivity_warned=FALSE, last_activity=NOW(), summary=jsonb_build_object('reopened',true) WHERE id=$1 RETURNING *", session_id)
        return _serialize(dict(row)) if row else None


# ── table_checks ──────────────────────────────────────────────────────────────

async def db_init_table_checks():
    """No-op: table_checks managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_create_checks(base_order_id: str, checks: list) -> list:
    """
    Replaces the ticket's 'open' checks and creates the new ones.
    checks = [{"check_number": 1, "items": [...], "subtotal": N, "tax_amount": N, "total": N}, ...]
    Each item in items: {"name": str, "qty": int, "unit_price": float, "subtotal": float}

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # tenant_connection() opens a transaction; conn.transaction() creates a SAVEPOINT.
        async with conn.transaction():
            # Delete only the checks still open (not the ones already paid)
            await conn.execute(
                "DELETE FROM table_checks WHERE base_order_id=$1 AND status='open'",
                base_order_id
            )
            inserted_ids = []
            for c in checks:
                check_id = f"{base_order_id}-CHK-{c['check_number']}"
                await conn.execute(
                    """INSERT INTO table_checks
                       (id, base_order_id, check_number, items,
                        subtotal, tax_amount, total)
                       VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                       ON CONFLICT (base_order_id, check_number)
                       DO UPDATE SET items=$4::jsonb, subtotal=$5,
                                     tax_amount=$6, total=$7, status='open'""",
                    check_id, base_order_id, int(c["check_number"]),
                    json.dumps(c["items"]),
                    to_decimal(c["subtotal"]), to_decimal(c["tax_amount"]), to_decimal(c["total"])
                )
                inserted_ids.append(check_id)
        rows = await conn.fetch(
            "SELECT * FROM table_checks WHERE base_order_id=$1 ORDER BY check_number",
            base_order_id
        )

    # Publish OUTSIDE the transaction block — tenant_connection() has already
    # committed by here. One event for the whole batch (this replaces the
    # entire open-checks set for the ticket in one call); entity_id is the
    # group id since no single check_id is more "the" event here.
    await _publish_check_group_updated(base_order_id)
    return [_serialize(dict(r)) for r in rows]


async def db_insert_check(
    base_order_id: str, check_number: int, items: list, subtotal, tax_amount, total,
) -> dict | None:
    """Insert ONE new check for a base_order_id WITHOUT touching any other
    check on the same ticket.

    db_create_checks (above) REPLACES the whole 'open' set for a
    base_order_id (DELETE + re-INSERT) — correct for a single caller that
    computes the full split in one shot (the bot's checkout flow, caja's
    split-checks editor), but destructive for an incremental caller: the
    re-INSERT only carries the 7 columns db_create_checks passes, so any
    OTHER already-open check silently loses its proposal_status/
    proposed_payments/tip_amount/proposal_customer_phone (they reset to
    NULL/default because the row was actually DELETED, not just matched by
    ON CONFLICT). The diner web-checkout flow (app/routes/diner.py) needs
    exactly that incremental behaviour — multiple diners can each request
    their own check over the life of one table, and an earlier diner's
    check may already carry a staff-visible pending proposal that must
    survive a later diner's request untouched.

    ON CONFLICT DO NOTHING (not DO UPDATE): a collision on (base_order_id,
    check_number) must never silently overwrite another check. Returns None
    on conflict — the caller (which computed check_number under its own
    distributed lock) should treat that as "state changed under me" rather
    than retry blindly.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        check_id = f"{base_order_id}-CHK-{check_number}"
        row = await conn.fetchrow(
            """INSERT INTO table_checks
                   (id, base_order_id, check_number, items, subtotal, tax_amount, total)
               VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
               ON CONFLICT (base_order_id, check_number) DO NOTHING
               RETURNING *""",
            check_id, base_order_id, check_number,
            json.dumps(items), to_decimal(subtotal), to_decimal(tax_amount), to_decimal(total),
        )

    # Publish OUTSIDE the transaction block — tenant_connection() has already
    # committed by here. Only on a genuine insert: ON CONFLICT DO NOTHING
    # means row is None when nothing actually changed.
    if row is not None:
        await _publish_check_updated(check_id, base_order_id)
    return _serialize(dict(row)) if row else None


async def db_get_checks(base_order_id: str) -> list:
    """Devuelve todos los checks de un ticket, con datos fiscales si existen.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch("""
            SELECT tc.*,
                   fi.cufe, fi.qr_data, fi.invoice_number, fi.dian_status,
                   fi.tax_regime, fi.tax_pct
            FROM table_checks tc
            LEFT JOIN fiscal_invoices fi ON fi.id = tc.fiscal_invoice_id
            WHERE tc.base_order_id = $1
            ORDER BY tc.check_number
        """, base_order_id)
    return [_serialize(dict(r)) for r in rows]


async def db_get_check(check_id: str) -> dict | None:
    """Devuelve un check individual.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_checks WHERE id=$1", check_id
        )
    return _serialize(dict(row)) if row else None


# Status transitions for table_checks (race-free payment flow):
#   open  ──claim──▶  paying  ──finalize──▶  invoiced
#                       │
#                       └────release────▶ open  (rollback when DIAN fails)
#
# Two cashiers paying the same check in parallel: only the FIRST claim
# transitions open→paying; the SECOND sees status='paying' and gets
# rejected upfront, before DIAN is even attempted.


async def _table_order_group_context(base_order_id: str) -> dict | None:
    """table_checks has no org_id/location_id/table_id of its own (it isn't
    an RLS table — scoped only via its base_order_id FK into table_orders).
    Resolve the (org_id, location_id, table_id) triple for an SSE event via
    that join, in a FRESH connection (called after the write's own
    tenant_connection() block has already committed and returned the
    connection to the pool)."""
    async with tenant_connection() as conn:
        return await conn.fetchrow(
            "SELECT org_id, branch_id, table_id FROM table_orders "
            "WHERE id=$1 OR base_order_id=$1 LIMIT 1",
            base_order_id,
        )


async def _publish_check_updated(check_id: str, base_order_id: str) -> None:
    """Best-effort: swallows lookup/publish errors (including odd shapes from
    unit tests that mock conn.fetchrow with a fixed, unrelated row sequence)
    so a check.updated publish failure can never fail the payment flow that
    just committed."""
    from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
    try:
        ctx = await _table_order_group_context(base_order_id)
        if ctx is None or ctx["org_id"] is None:
            return
        await realtime.publish(
            ctx["org_id"], "check.updated",
            location_id=ctx["branch_id"], table_id=ctx["table_id"], entity_id=check_id,
        )
    except Exception:
        log.warning("realtime.check_updated.publish_failed", check_id=check_id, exc_info=True)


async def _publish_check_group_updated(base_order_id: str) -> None:
    """Like _publish_check_updated, for a write that touches the WHOLE open-
    checks set of a ticket at once (db_create_checks) rather than a single
    check_id — entity_id is the group id itself."""
    await _publish_check_updated(base_order_id, base_order_id)


async def _publish_table_order_group_updated(base_order_id: str) -> None:
    """Same context resolution as _publish_check_updated, for the
    table_order.updated event fired when finalizing payment closes the whole
    group (table_orders.status -> 'factura_entregada')."""
    from app.services import realtime  # noqa: PLC0415 — avoid import cycle at module load
    try:
        ctx = await _table_order_group_context(base_order_id)
        if ctx is None or ctx["org_id"] is None:
            return
        await realtime.publish(
            ctx["org_id"], "table_order.updated",
            location_id=ctx["branch_id"], table_id=ctx["table_id"], entity_id=base_order_id,
        )
    except Exception:
        log.warning("realtime.table_order_updated.publish_failed", base_order_id=base_order_id, exc_info=True)


async def db_claim_check_for_payment(check_id: str, base_order_id: str) -> dict | None:
    """Atomically claim a check for payment processing.

    Returns the full check row if the claim succeeded (status open → paying).
    Returns None if the check is missing, doesn't belong to the given order,
    or is in any state other than 'open' (already paying / invoiced / cancelled).

    The caller MUST follow up with EITHER:
      - db_finalize_check_payment(...) — commits the payment (paying → invoiced).
      - db_release_check(check_id)     — rolls back the claim (paying → open),
        e.g. if DIAN invoice generation fails.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        async with conn.transaction():
            # SELECT FOR UPDATE serializes concurrent pay_check calls on this row.
            # The second cashier blocks here until the first commits, then sees
            # status='paying' and the UPDATE below returns 0 rows.
            row = await conn.fetchrow(
                """SELECT * FROM table_checks
                   WHERE id=$1 AND base_order_id=$2
                   FOR UPDATE""",
                check_id, base_order_id,
            )
            if not row or row["status"] != "open":
                return None
            await conn.execute(
                "UPDATE table_checks SET status='paying' WHERE id=$1",
                check_id,
            )
    # Return the row content as it was BEFORE the claim (for downstream use).
    # We don't refetch — the caller only needs the immutable fields (items, total, etc.).
    claimed = _serialize(dict(row))
    claimed["status"] = "paying"
    await _publish_check_updated(check_id, base_order_id)
    return claimed


async def db_release_check(check_id: str) -> bool:
    """Roll back a claim: paying → open.

    Returns True if the rollback applied (check was 'paying'), False otherwise.
    Used as compensation when DIAN invoice generation fails after the claim.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        result = await conn.fetchrow(
            "UPDATE table_checks SET status='open' WHERE id=$1 AND status='paying' "
            "RETURNING id, base_order_id",
            check_id,
        )
    if result is not None:
        await _publish_check_updated(check_id, result["base_order_id"])
    return result is not None


async def db_finalize_check_payment(
    check_id: str,
    base_order_id: str,
    payments: list,
    change_amount: float,
    fiscal_invoice_id: int,
    customer_name: str = None,
    customer_nit: str = None,
    customer_email: str = None,
    tip_amount: float = 0.0,
) -> bool:
    """
    Atomically:
    1. Updates the check from status='paying' to status='invoiced' with payments and change.
    2. If ALL checks for the base_order_id are in {invoiced, cancelled},
       updates table_orders to status='factura_entregada'.

    Returns True if the finalize succeeded, False if the check was not in
    'paying' state (e.g. already finalized by a concurrent call). The caller
    should treat False as a 409 — DO NOT proceed with
    customer-facing side effects on False.

    Pre-condition: caller has previously called db_claim_check_for_payment
    successfully (which transitioned status open → paying).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    group_closed = False
    async with tenant_connection() as conn:
        # tenant_connection() opens a transaction; conn.transaction() creates a SAVEPOINT.
        async with conn.transaction():
            updated = await conn.fetchrow(
                """UPDATE table_checks
                   SET payments=$1::jsonb, change_amount=$2,
                       fiscal_invoice_id=$3, status='invoiced',
                       customer_name=$4, customer_nit=$5, customer_email=$6,
                       tip_amount=$7, paid_at=NOW()
                   WHERE id=$8 AND status='paying'
                   RETURNING id""",
                json.dumps(payments), to_decimal(change_amount),
                fiscal_invoice_id,
                customer_name, customer_nit, customer_email,
                to_decimal(tip_amount), check_id
            )
            if updated is None:
                # Concurrent finalize already happened, or claim was lost.
                # Caller will detect via False return and stop downstream work.
                return False
            # Mark proposal as confirmed if it existed
            await conn.execute(
                """UPDATE table_checks
                   SET proposal_status = 'confirmed'
                 WHERE id = $1 AND proposal_status IS NOT NULL""",
                check_id
            )
            # Check whether all checks in the group are closed
            pending = await conn.fetchval(
                """SELECT COUNT(*) FROM table_checks
                   WHERE base_order_id=$1
                     AND status NOT IN ('invoiced','cancelled')""",
                base_order_id
            )
            if pending == 0:
                await conn.execute(
                    """UPDATE table_orders
                       SET status='factura_entregada', updated_at=NOW()
                       WHERE (id=$1 OR base_order_id=$1)
                         AND status NOT IN ('cancelado','factura_entregada')""",
                    base_order_id
                )
                group_closed = True

    # Publish OUTSIDE the transaction block — tenant_connection() has already
    # committed by here. check.updated always; table_order.updated too when
    # this finalize just closed the whole group (kitchen/waiter/cashier
    # screens all key off table_orders.status).
    await _publish_check_updated(check_id, base_order_id)
    if group_closed:
        await _publish_table_order_group_updated(base_order_id)
    return True


async def db_delete_open_check(check_id: str) -> bool:
    """Deletes a check only if it's in 'open' status. Returns True if deleted.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "DELETE FROM table_checks WHERE id=$1 AND status='open' RETURNING base_order_id",
            check_id,
        )

    if row is not None:
        # Publish OUTSIDE the transaction block — tenant_connection() has
        # already committed by here.
        await _publish_check_updated(check_id, row["base_order_id"])
    return row is not None


async def db_attach_proposal(
    check_id: str,
    proposed_payments: list,
    proposed_tip: float,
    proposal_source: str,
    proposal_status: str,
    customer_phone: str,
) -> None:
    """Attaches payment proposal metadata to an existing check.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """UPDATE table_checks
               SET proposed_payments     = $2::jsonb,
                   proposed_tip          = $3,
                   proposal_source       = $4,
                   proposal_status       = $5,
                   proposal_customer_phone = $6,
                   proposal_created_at   = NOW()
             WHERE id = $1
             RETURNING base_order_id""",
            check_id,
            json.dumps(proposed_payments),
            to_decimal(proposed_tip),
            proposal_source,
            proposal_status,
            customer_phone,
        )

    if row is not None:
        await _publish_check_updated(check_id, row["base_order_id"])


async def db_set_check_tip(check_id: str, tip_amount: float) -> None:
    """Updates tip_amount on an open check (during the bot's checkout flow).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE table_checks SET tip_amount = $1 WHERE id = $2",
            to_decimal(tip_amount),
            check_id,
        )


async def db_attach_proof(base_order_id: str, customer_phone: str, media_url: str) -> bool:
    """
    Attaches a proof URL to the customer's checks with an awaiting_proof proposal.
    Returns True if at least one check was updated.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        result = await conn.execute(
            """UPDATE table_checks
               SET proof_media_url  = $3,
                   proposal_status  = 'proof_received'
             WHERE base_order_id     = $1
               AND proposal_customer_phone = $2
               AND proposal_status   = 'awaiting_proof'""",
            base_order_id,
            customer_phone,
            media_url,
        )
    return result != "UPDATE 0"


async def db_get_open_proposal_for_phone(
    restaurant_id: int, customer_phone: str
) -> dict | None:
    """
    Looks up whether the customer has any check with a pending/awaiting-proof proposal.
    Useful in chat.py to intercept images and attach them without going through the LLM.
    Returns the check (with base_order_id) or None.

    Caller passes restaurant_id = the org_id (post-Wave-2 db_get_restaurant_by_phone
    normalises restaurants.id → org_id). The legacy filter `tor.branch_id = $1`
    was the SAME bug pattern that broke /floor-plan and /dashboard/conversations:
    after migration 0057 synced branch_id := location_id (1, 3, 4 for a multi-
    sede org), `branch_id = 8` matched zero rows. As a result the bot's
    proof-shortcut in chat.py (msg_type=='image') never fired and the LLM kept
    asking the customer to send the proof again. Fix: filter by org_id.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """SELECT tc.id, tc.base_order_id, tc.proposal_status, tc.proposed_payments
               FROM table_checks tc
               JOIN table_orders tor ON tor.base_order_id = tc.base_order_id
              WHERE tc.proposal_customer_phone = $2
                AND tc.proposal_status IN ('pending', 'awaiting_proof')
                AND tor.org_id = $1
              ORDER BY tc.proposal_created_at DESC
              LIMIT 1""",
            restaurant_id,
            customer_phone,
        )
    return _serialize(dict(row)) if row else None


async def db_list_checkout_proposals(
    restaurant_id: int, branch_ids: list[int] | None = None
) -> list:
    """
    Lists tables that have checks with active bot proposals (pending/awaiting_proof/proof_received).
    Grouped by base_order_id for the Cashier view.

    Caller passes restaurant_id = org_id. branch_ids (when given) is a list
    of location_ids to narrow down within the org. When omitted we filter
    by org_id alone (cross-sede view).

    Pre-2026-04-29 the SQL filtered `tor.branch_id = ANY($1::int[])` with
    `[org_id]` as the default — broken post-migration 0057 (branch_id ==
    location_id). Same bug family as db_get_open_proposal_for_phone above.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_ids:
            # Specific sedes (location_ids) within the org
            sql = (
                """SELECT DISTINCT ON (tor.table_name, COALESCE(tor.branch_id, 0))
                     tor.base_order_id,
                     tor.table_name,
                     tor.total           AS order_total,
                     tor.branch_id,
                     json_agg(tc.* ORDER BY tc.check_number) AS checks
                   FROM table_orders tor
                   JOIN table_checks tc ON tc.base_order_id = tor.base_order_id
                  WHERE tor.org_id = $1
                    AND tor.branch_id = ANY($2::int[])
                    AND tc.proposal_status IN ('pending', 'awaiting_proof', 'proof_received')
                    AND tc.status = 'open'
                  GROUP BY tor.base_order_id, tor.table_name, tor.total, tor.branch_id
                  ORDER BY tor.table_name, COALESCE(tor.branch_id, 0),
                           MIN(tc.proposal_created_at) DESC"""
            )
            rows = await conn.fetch(sql, restaurant_id, branch_ids)
        else:
            sql = (
                """SELECT DISTINCT ON (tor.table_name, COALESCE(tor.branch_id, 0))
                     tor.base_order_id,
                     tor.table_name,
                     tor.total           AS order_total,
                     tor.branch_id,
                     json_agg(tc.* ORDER BY tc.check_number) AS checks
                   FROM table_orders tor
                   JOIN table_checks tc ON tc.base_order_id = tor.base_order_id
                  WHERE tor.org_id = $1
                    AND tc.proposal_status IN ('pending', 'awaiting_proof', 'proof_received')
                    AND tc.status = 'open'
                  GROUP BY tor.base_order_id, tor.table_name, tor.total, tor.branch_id
                  ORDER BY tor.table_name, COALESCE(tor.branch_id, 0),
                           MIN(tc.proposal_created_at) DESC"""
            )
            rows = await conn.fetch(sql, restaurant_id)
    return [_serialize(dict(r)) for r in rows]


async def db_cancel_checkout_proposal(base_order_id: str) -> None:
    """Cancels all pending bot checkout proposals on a base order's open checks.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            """UPDATE table_checks
               SET proposal_status = NULL,
                   proposed_payments = NULL,
                   proposed_tip = NULL,
                   proof_media_url = NULL,
                   proposal_customer_phone = NULL,
                   proposal_created_at = NULL
             WHERE base_order_id = $1
               AND proposal_status IS NOT NULL
               AND status = 'open'""",
            base_order_id,
        )


async def db_get_check_ticket(check_id: str) -> dict | None:
    """Returns check data + fiscal info for invoice printing.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow("""
            SELECT tc.id, tc.base_order_id, tc.check_number,
                   tc.items, tc.subtotal, tc.tax_amount, tc.total,
                   tc.payments, tc.change_amount, tc.status,
                   tc.customer_name, tc.customer_nit,
                   tc.created_at, tc.paid_at,
                   fi.cufe, fi.qr_data, fi.invoice_number,
                   fi.dian_status, fi.tax_regime, fi.tax_pct,
                   to2.table_name
            FROM table_checks tc
            LEFT JOIN fiscal_invoices fi ON fi.id = tc.fiscal_invoice_id
            LEFT JOIN table_orders to2  ON to2.id = tc.base_order_id
            WHERE tc.id = $1
        """, check_id)
    if not row:
        return None
    d = _serialize(dict(row))
    if isinstance(d.get("items"), str):
        d["items"] = json.loads(d["items"])
    if isinstance(d.get("payments"), str):
        d["payments"] = json.loads(d["payments"])
    return d


# ── Floor plan & table properties (Apparta integration) ─────────────────────

async def db_update_table_properties(table_id: str, **kwargs):
    """Update table properties (capacity, table_type, zone). Only updates provided fields.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    allowed = {"name", "capacity", "table_type", "zone"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return None
    async with tenant_connection() as conn:
        sets = []
        vals = []
        for i, (col, val) in enumerate(updates.items(), 1):
            sets.append(f"{col}=${i}")
            vals.append(val)
        vals.append(table_id)
        sql = f"UPDATE restaurant_tables SET {', '.join(sets)} WHERE id=${len(vals)} RETURNING *"
        row = await conn.fetchrow(sql, *vals)
        return _serialize(dict(row)) if row else None


async def db_update_table_position(table_id: str, position_x: float, position_y: float):
    """Update table position for floor plan.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "UPDATE restaurant_tables SET position_x=$1, position_y=$2 WHERE id=$3 RETURNING *",
            position_x, position_y, table_id
        )
        return _serialize(dict(row)) if row else None


# Whitelist of mutable columns for bulk floor plan save. Must match the
# per-table PUT /properties + /position endpoints (single source of truth for
# what the editor can persist). Adding a column here requires a corresponding
# Pydantic field in FloorPlanTableUpdate (routes/tables.py).
_FLOOR_PLAN_BULK_COLS: tuple[str, ...] = (
    "position_x", "position_y", "capacity", "table_type", "zone", "name",
)


async def db_save_floor_plan_bulk(org_id: int, tables: list[dict]) -> dict:
    """Bulk update floor plan layout.

    Each entry must contain `id` (str — restaurant_tables.id is TEXT). Other
    fields are optional and only those present (non-None) get persisted —
    PATCH semantics, mirroring the per-table PUT endpoints.

    Tenant isolation: every UPDATE pins org_id explicitly in the WHERE clause
    so that tables belonging to another org cannot be silently mutated, even
    in the unlikely case of an RLS policy regression. Cross-org attempts
    surface in `errors[]` with reason='not_found_or_wrong_org'. Combined
    with the FORCE RLS policy on restaurant_tables this is belt-and-
    suspenders, but the explicit guard documents intent.

    Returns: {updated: int, skipped: int, errors: [{table_id, reason}]}.

    # Requires active tenant_scope(org_id) or bypass_tenant_scope().
    """
    updated = 0
    skipped = 0
    errors: list[dict] = []

    async with tenant_connection() as conn:
        for entry in tables:
            tid = entry.get("id")
            if not isinstance(tid, str) or not tid:
                errors.append({"table_id": tid, "reason": "invalid_id"})
                continue

            sets: list[str] = []
            vals: list = []
            idx = 1
            for col in _FLOOR_PLAN_BULK_COLS:
                if col in entry and entry[col] is not None:
                    sets.append(f"{col}=${idx}")
                    vals.append(entry[col])
                    idx += 1

            if not sets:
                skipped += 1
                continue

            vals.append(tid)
            tid_pos = idx
            vals.append(org_id)
            org_pos = idx + 1

            sql = (
                f"UPDATE restaurant_tables SET {', '.join(sets)} "
                f"WHERE id = ${tid_pos} AND org_id = ${org_pos}"
            )
            result = await conn.execute(sql, *vals)
            if result == "UPDATE 0":
                errors.append({"table_id": tid, "reason": "not_found_or_wrong_org"})
            else:
                updated += 1

    return {"updated": updated, "skipped": skipped, "errors": errors}


async def db_get_floor_plan(branch_id: int = None):
    """Get all tables with positions and current occupancy for floor plan view.

    Note on the `branch_id` param: kept for caller back-compat, but the SQL
    actually filters by `location_id` (Wave-2 canonical sede id). The
    `branch_id` column is a legacy alias post-migration 0057 and is no
    longer the canonical filter. Same fix as db_get_tables (commit 2fdc124).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        sql = """
            SELECT t.*,
                   s.id AS session_id,
                   s.phone AS session_phone,
                   s.status AS session_status,
                   CASE WHEN s.id IS NOT NULL AND s.status = 'active' THEN TRUE ELSE FALSE END AS occupied
            FROM restaurant_tables t
            LEFT JOIN table_sessions s ON s.table_id = t.id AND s.status = 'active'
            WHERE t.active = TRUE
        """
        params = []
        if branch_id is not None:
            sql += " AND t.location_id = $1"
            params.append(branch_id)
        sql += " ORDER BY t.zone, t.number"
        rows = await conn.fetch(sql, *params)
        return [_serialize(dict(r)) for r in rows]


async def db_get_session_phones_by_branch(branch_id: int, org_id: int) -> set:
    """
    Return the set of phone numbers that have table sessions in a given branch.
    Used by stats.py to filter conversations to those belonging to branch tables.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ts.phone
            FROM table_sessions ts
            JOIN restaurant_tables rt ON ts.table_id = rt.id
            WHERE rt.branch_id = $1 AND ts.org_id = $2
            """,
            branch_id, org_id,
        )
    return {r["phone"] for r in rows}


async def db_get_active_session_table_ids() -> set:
    """Return set of table_ids that have active or nps_pending sessions.

    Cross-tenant scheduler function — iterates ALL tenants.
    # Uses bypass_tenant_scope internally (scheduler admin operation).
    """
    with bypass_tenant_scope("scheduler: active session table_ids sweep across all tenants"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                "SELECT table_id FROM table_sessions WHERE status IN ('active','nps_pending')"
            )
    return {r["table_id"] for r in rows}


async def db_get_pending_orders_by_branch(branch_id: int) -> list:
    """Return table_id + status for non-closed orders in a branch (for POS tables-status).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            # branch_id-guard-allow: location_id passed by caller; table_orders.branch_id == location_id post-0057
            "SELECT table_id, status FROM table_orders "
            "WHERE status NOT IN ('factura_entregada', 'cancelado') AND branch_id = $1",
            branch_id,
        )
    return [dict(r) for r in rows]


async def db_get_tables_status_enrichment(branch_id: int) -> dict:
    """Return per-table enrichment data for /api/pos/tables-status.

    Returns a dict keyed by table_id with fields:
      has_waiter_alert:   bool
      has_open_check:     bool
      current_total:      float
      session_active:     bool
      session_started_at: str|None   (ISO 8601)
      active_order_id:    str|None   (most recent non-closed table_order on this table)
      channel:            str|None   (whatsapp_bot | pos | qr_pickup | web | manual)
      waiter_staff_id:    str|None   (from table_orders.waiter_staff_id)
      assigned_staff_id:  str|None   (from table_sessions.assigned_staff_id)
      waiter_name:        str|None   (friendly name; prefers waiter_staff_id else assigned_staff_id)

    Uses a DISTINCT ON CTE to pick the most recent active table_order per table,
    then LEFT JOINs staff twice (once per role) for the display name. Requires
    active tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            WITH active_order AS (
                SELECT DISTINCT ON (table_id)
                    table_id,
                    id              AS order_id,
                    waiter_staff_id,
                    channel
                FROM table_orders
                WHERE branch_id = $1
                  AND status NOT IN ('factura_entregada', 'cancelado')
                ORDER BY table_id, created_at DESC
            ),
            active_session AS (
                SELECT DISTINCT ON (table_id)
                    table_id,
                    assigned_staff_id,
                    started_at
                FROM table_sessions
                WHERE status IN ('active', 'nps_pending')
                ORDER BY table_id, started_at DESC
            ),
            -- Aggregated on its own: summed after the alert/order joins, one
            -- open check was counted once per alert row (a table with two
            -- alerts showed twice its bill — found 2026-09-28).
            open_checks AS (
                SELECT tord.table_id,
                       COUNT(tc.id)   AS n_open,
                       SUM(tc.total)  AS total
                FROM table_orders tord
                JOIN table_checks tc ON tc.base_order_id = tord.id
                                    AND tc.status = 'open'
                WHERE tord.branch_id = $1
                  AND tord.status NOT IN ('factura_entregada', 'cancelado')
                GROUP BY tord.table_id
            )
            SELECT
                rt.id                              AS table_id,
                EXISTS (
                    SELECT 1 FROM waiter_alerts wa
                    WHERE wa.table_id = rt.id AND wa.dismissed = FALSE
                )                                  AS has_waiter_alert,
                COALESCE(oc.n_open, 0) > 0         AS has_open_check,
                COALESCE(oc.total, 0)              AS current_total,
                (asess.table_id IS NOT NULL)       AS session_active,
                asess.started_at                   AS session_started_at,
                ao.order_id                        AS active_order_id,
                ao.channel                         AS channel,
                ao.waiter_staff_id::text           AS waiter_staff_id,
                asess.assigned_staff_id::text      AS assigned_staff_id,
                COALESCE(sw.name, sa.name)         AS waiter_name
            FROM restaurant_tables rt
            LEFT JOIN active_session  asess ON asess.table_id = rt.id
            LEFT JOIN active_order    ao    ON ao.table_id = rt.id
            LEFT JOIN open_checks     oc    ON oc.table_id = rt.id
            LEFT JOIN staff           sw    ON sw.id = ao.waiter_staff_id
            LEFT JOIN staff           sa    ON sa.id = asess.assigned_staff_id
            WHERE rt.branch_id = $1
            """,
            branch_id,
        )
    result = {}
    for r in rows:
        started = r["session_started_at"]
        result[r["table_id"]] = {
            "has_waiter_alert":   bool(r["has_waiter_alert"]),
            "has_open_check":     bool(r["has_open_check"]),
            "current_total":      float(r["current_total"] or 0),
            "session_active":     bool(r["session_active"]),
            "session_started_at": started.isoformat() + "Z" if started else None,
            "active_order_id":    r["active_order_id"],
            "channel":            r["channel"],
            "waiter_staff_id":    r["waiter_staff_id"],
            "assigned_staff_id":  r["assigned_staff_id"],
            "waiter_name":        r["waiter_name"],
        }
    return result


async def db_delete_waiter_alert(alert_id: int) -> None:
    """Hard-delete a waiter alert by id.

    BUG FOUND 2026-09 (fixed in the same pass as the dismiss IDOR/admin-call
    audit): this function used to be named `db_dismiss_waiter_alert`,
    duplicating the name of the OTHER function earlier in this file (the
    real soft-dismiss: `UPDATE waiter_alerts SET dismissed=TRUE ...`). Python
    silently keeps only the LAST definition of a module-level name, so every
    call to `db_dismiss_waiter_alert` — including the live
    POST /api/waiter-alerts/{id}/dismiss endpoint — was actually HARD
    DELETING the row instead of setting `dismissed=TRUE`, permanently losing
    alert history and making the `dismissed` column effectively dead (no row
    survives long enough to ever read `dismissed=TRUE`). Renamed here to
    restore the real dismiss function's reachability; this delete variant
    had zero callers under its old name (it never could — it was shadowed)
    so it is kept as a distinct, explicitly-named utility rather than
    removed outright.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute("DELETE FROM waiter_alerts WHERE id = $1", alert_id)


async def db_get_table_orders_for_branch(
    branch_id: int | None,
    status: str | None = None,
    is_admin: bool = False,
    org_id: int | None = None,
) -> list:
    """
    Return table orders filtered by org/sede and optional status.

    Param semantics post-2026-04-29:
      - org_id (preferred): caller passes the tenant org_id. SQL filters
        `tor.org_id = $org_id` — works regardless of legacy branch_id state.
      - branch_id: when given, treated as a SEDE id (location_id post-Wave-2).
        Pre-2026-04-29 callers passed user.branch_id which is the org_id —
        the route now resolves that case into org_id and leaves branch_id None.
      - is_admin + branch_id None + org_id None: returns all orders across all
        tenants (legacy admin view; only safe under bypass_tenant_scope).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        conditions: list[str] = []
        params: list = []
        idx = 1
        if status:
            conditions.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        else:
            conditions.append("status NOT IN ('factura_entregada','cancelado')")
        if org_id is not None:
            conditions.append(f"org_id = ${idx}")
            params.append(org_id)
            idx += 1
        if branch_id is not None:
            # Post-0057 branch_id == location_id; we filter the canonical column
            # to be future-proof if branch_id ever drifts again.
            conditions.append(f"location_id = ${idx}")
            params.append(branch_id)
            idx += 1
        # Safety: if neither org_id nor branch_id given AND caller is not admin,
        # return empty (avoid leaking all orders cross-tenant).
        if org_id is None and branch_id is None and not is_admin:
            return []
        sql = "SELECT * FROM table_orders WHERE " + " AND ".join(conditions) + " ORDER BY created_at ASC"
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def db_get_table_orders_by_base_id(
    order_id: str,
    branch_id: int | None = None,
) -> list:
    """
    Return all orders belonging to a base_order_id group (for ticket aggregation).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            rows = await conn.fetch(
                # branch_id-guard-allow: location_id passed by caller; table_orders.branch_id == location_id post-0057
                """SELECT * FROM table_orders
                   WHERE (id = $1 OR base_order_id = $1) AND branch_id = $2
                   ORDER BY created_at ASC""",
                order_id, branch_id,
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM table_orders
                   WHERE id = $1 OR base_order_id = $1
                   ORDER BY created_at ASC""",
                order_id,
            )
    return [dict(r) for r in rows]


async def db_adjust_table_bill(
    base_order_id: str,
    adjusted_items: list,
    new_total,
) -> bool:
    """
    Update base order with adjusted items/total and zero out sub-orders.
    Returns False if the base order does not exist.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    import json as _json
    async with tenant_connection() as conn:
        base_row = await conn.fetchrow(
            "SELECT id FROM table_orders WHERE id=$1", base_order_id
        )
        if not base_row:
            return False
        await conn.execute(
            "UPDATE table_orders SET items=$2, total=$3, updated_at=NOW() WHERE id=$1",
            base_order_id, _json.dumps(adjusted_items), new_total,
        )
        await conn.execute(
            "UPDATE table_orders SET items='[]'::jsonb, total=0, updated_at=NOW() WHERE base_order_id=$1 AND id != $1",
            base_order_id,
        )
    return True


async def db_get_table_order_record(order_id: str) -> dict | None:
    """Return phone, table_name, base_order_id, table_id, org_id, location_id
    for a table order.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT phone, table_name, base_order_id, table_id, org_id, location_id "
            "FROM table_orders WHERE id=$1",
            order_id,
        )
    return dict(row) if row else None


async def db_get_open_table_session_by_phone(phone: str) -> dict | None:
    """Return the active table session for a phone, or None.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL",
            phone,
        )
    return dict(row) if row else None


async def db_verify_table_in_restaurant(table_id: str, rest_id: int) -> dict | None:
    """Return branch_id for a table, or None if not found.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        return await conn.fetchrow(
            "SELECT branch_id FROM restaurant_tables WHERE id = $1", table_id
        )


async def db_verify_branch_is_child(branch_id: int, parent_id: int) -> bool:
    """Return True if branch_id is a direct child of parent_id.

    Queries `restaurants` (no RLS on this table) — safe to run within any
    tenant_scope or bypass_tenant_scope. set_config does not affect this query.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM locations
            WHERE id = $1
              AND org_id = (SELECT org_id FROM locations WHERE id = $2)
              AND id != $2
            """,
            branch_id, parent_id,
        )
    return row is not None


async def db_get_delivery_orders_for_cashier(org_id: int, location_id: int | None = None) -> list:
    """
    Return pending delivery/pickup orders for the kitchen/caja view (last 24h,
    excluding terminal statuses).

    Bug fix (chunk 4, docs/claude/delivery-web.md): the web delivery/pickup
    wave introduced two new pre-kitchen statuses — 'pendiente_aceptacion'
    (the cashier hasn't accepted yet) and 'rechazado' (the cashier rejected
    it) — that did not exist when this exclusion list was written. Neither
    was excluded, so BEFORE the cashier accepts a web order it was already
    visible on this same kitchen/caja screen (violating "only after
    acceptance does the ticket reach the kitchen KDS",
    docs/claude/delivery-web.md "Order lifecycle"), and a REJECTED order
    would keep showing here for the rest of its 24h window. Found while
    verifying that POST /api/staff/delivery/orders/{id}/accept actually
    releases the ticket to this screen — see tests/test_delivery_cashier.py.

    location_id (chunk 8, "Known open items"): this feed was org-scoped
    only, so every kitchen of a multi-sede org saw every sede's delivery
    tickets — same gap class as the waiter alerts (memory:
    mesero-location-gap). None (the default) keeps the old org-wide
    behaviour for callers that legitimately want every sede (an admin with
    no X-Location-ID header — see app/routes/tables.py); a concrete int
    filters to just that sede.

    # Requires active tenant_scope(org_id). The explicit org_id filter is
    # defense in depth: RLS does not apply to a superuser connection.
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT * FROM orders
               WHERE order_type IN ('domicilio','recoger')
               AND created_at >= NOW() - INTERVAL '24 hours'
               AND status NOT IN (
                   'pendiente_aceptacion', 'rechazado',
                   'en_camino', 'en_puerta', 'entregado', 'cancelado'
               )
               AND org_id = $1
               AND ($2::bigint IS NULL OR location_id = $2)
               ORDER BY created_at DESC""",
            org_id, location_id,
        )
    return [dict(r) for r in rows]


async def db_get_delivery_status_hash_for_restaurant(restaurant_id: int) -> list:
    """Return id+status pairs for active delivery orders scoped to a restaurant.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """
            SELECT o.id, o.status
            FROM orders o
            WHERE o.order_type IN ('domicilio', 'recoger')
              AND o.status IN ('pendiente', 'confirmado', 'en_preparacion', 'listo', 'en_camino', 'en_puerta')
              AND o.org_id = $1
            ORDER BY o.id
            """,
            restaurant_id,
        )
    return [dict(r) for r in rows]


async def db_update_delivery_order_status(order_id: str, new_status: str) -> None:
    """Update delivery order status.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute("UPDATE orders SET status=$2 WHERE id=$1", order_id, new_status)


async def db_get_delivery_order_full(order_id: str) -> dict | None:
    """Return full order row for billing/DIAN processing.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
    return dict(row) if row else None


async def db_force_delete_conversation_data(phone: str, username: str) -> None:
    """
    Force-delete conversation, cart, and close active table session for a phone.
    Called from the manual chat cleanup endpoint.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone = $1", phone)
        await conn.execute("DELETE FROM carts WHERE phone = $1", phone)
        await conn.execute(
            "UPDATE table_sessions SET status = 'closed', closed_at = NOW(), "
            "closed_by = 'manual_delete', closed_by_username = $2 "
            "WHERE phone = $1 AND closed_at IS NULL",
            phone, username,
        )


async def db_get_closed_sessions(hours: int, org_id: int | None) -> list[dict]:
    """Return closed table sessions within the given hours window.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if org_id:
            rows = await conn.fetch(
                "SELECT * FROM table_sessions WHERE closed_at IS NOT NULL"
                " AND closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
                " AND org_id = $2 ORDER BY closed_at DESC",
                hours, org_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM table_sessions WHERE closed_at IS NOT NULL"
                " AND closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
                " ORDER BY closed_at DESC",
                hours,
            )
    return [dict(r) for r in rows]


async def db_get_session_with_history(session_id: int) -> tuple[dict | None, list]:
    """Return (session_dict, conversation_history) for a table session.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    import json as _json
    async with tenant_connection() as conn:
        session = await conn.fetchrow("SELECT * FROM table_sessions WHERE id = $1", session_id)
        if not session:
            return None, []
        conv = await conn.fetchrow(
            "SELECT history FROM conversations WHERE phone = $1", session["phone"]
        )
    history: list = []
    if conv and conv["history"]:
        try:
            history = _json.loads(conv["history"]) if isinstance(conv["history"], str) else conv["history"]
        except Exception:
            pass
    return dict(session), history


async def db_reopen_session(session_id: int) -> None:
    """Clear closed_at / closed_by fields to re-open a table session.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE table_sessions SET closed_at = NULL, closed_by = NULL, closed_by_username = NULL WHERE id = $1",
            session_id,
        )


async def db_get_recent_table_orders_by_phone(
    org_id: int,
    phone: str,
    limit: int = 5,
) -> list[dict]:
    """Return the most recent table (salon) orders for a phone number.

    Runs under the already-active tenant_scope set by the call site.
    Returns a list of dicts:
        {"id", "total": float, "created_at": str | None, "items_summary": str, "source": "table"}
    """
    import json as _json  # noqa: PLC0415

    results: list[dict] = []
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT id, total, created_at, items
               FROM table_orders
               WHERE phone = $1
                 AND org_id = $2
               ORDER BY created_at DESC
               LIMIT $3""",
            phone, org_id, limit,
        )
    for r in rows:
        items_raw = r["items"]
        if isinstance(items_raw, str):
            try:
                items_raw = _json.loads(items_raw)
            except Exception:
                items_raw = []
        items_list = items_raw if isinstance(items_raw, list) else []
        summary = " · ".join(i.get("name", "item") for i in items_list[:3])
        if len(items_list) > 3:
            summary += f" +{len(items_list) - 3}"
        results.append({
            "id": f"#{str(r['id'])[-6:].upper()}",
            "total": float(to_decimal(r["total"])),  # JSON boundary
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "items_summary": summary,
            "source": "table",
        })
    return results


async def db_session_alert_waiter(session_id: int, message: str) -> bool:
    """
    Look up the table session and insert a waiter alert for its table.
    Returns True if the session was found, False otherwise.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        session = await conn.fetchrow("SELECT * FROM table_sessions WHERE id = $1", session_id)
        if session:
            await conn.execute(
                "INSERT INTO waiter_alerts (table_id, table_name, message, status, org_id) "
                "VALUES ($1, $2, $3, 'active', NULLIF(current_setting('app.org_id', true), '')::bigint)",
                session["table_id"], session["table_name"], message,
            )
            return True
    return False


# ── Capa 3: Anti-impostor (pending_table_validation) ────────────────────────

async def db_session_is_verified(phone: str, org_id: int) -> bool:
    """Return True if the active session for this phone+bot is verified.

    Used by the bot to decide whether a place_order should set
    pending_table_validation=true (unverified session) or go straight
    to the kitchen (verified session or no session at all).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT verified FROM table_sessions "
            "WHERE phone=$1 AND org_id=$2 AND status='active' "
            "ORDER BY started_at DESC LIMIT 1",
            phone,
            org_id,
        )
        if row is None:
            # No active session — no pending hold needed.
            return True
        return bool(row["verified"])


async def db_session_has_prior_orders(phone: str, org_id: int) -> bool:
    """Return True if the active session for this phone already has at least
    one table_order that is NOT pending_table_validation.

    Used to allow the second+ order in an unverified session to go through
    without hold (once the first order is already pending, the waiter will
    come to confirm — no need to hold subsequent ones).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        session = await conn.fetchrow(
            "SELECT table_id FROM table_sessions "
            "WHERE phone=$1 AND org_id=$2 AND status='active' "
            "ORDER BY started_at DESC LIMIT 1",
            phone,
            org_id,
        )
        if session is None:
            return False
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM table_orders "
            "WHERE table_id=$1 AND phone=$2 "
            "  AND status NOT IN ('cancelado') ",
            session["table_id"],
            phone,
        )
        return int(count or 0) > 0


async def db_confirm_table_real(table_id: str, org_id: int, mesero_username: str) -> int:
    """Waiter confirms the customer is real at this table.

    1. Updates all pending_table_validation orders on the table to false
       (releases them to the kitchen queue).
    2. Sets table_sessions.verified=true for active sessions on this table.

    Returns the count of orders released to the kitchen.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        released = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE table_orders
                SET    pending_table_validation = false,
                       updated_at = NOW()
                WHERE  table_id = $1
                  AND  pending_table_validation = true
                  AND  status NOT IN ('cancelado', 'entregado', 'factura_entregada')
                RETURNING id
            )
            SELECT COUNT(*) FROM updated
            """,
            table_id,
        )
        await conn.execute(
            """
            UPDATE table_sessions
            SET    verified = true
            WHERE  table_id = $1
              AND  status = 'active'
            """,
            table_id,
        )

    from app.services.logging import get_logger as _gl
    _log = _gl("tables_repo")
    _log.info(
        "table.confirmed_real",
        table_id=table_id,
        org_id=org_id,
        confirmed_by=mesero_username,
        orders_released=int(released or 0),
    )
    return int(released or 0)


async def db_mark_session_verified(session_id: int) -> None:
    """Mark a single table_sessions row as verified (Capa 3 anti-impostor
    bypass). Used by the Mesio-native diner web-chat flow: a diner who
    scanned the physical table QR (or supplied the correct join_code to
    join an already-open table) is, by construction, MORE certain to be at
    the real table than the WhatsApp geo-claim flow db_confirm_table_real
    guards against — so their first order should go straight to the
    kitchen (product decision: no waiter-approval step for web orders).

    Unlike db_confirm_table_real (which verifies every active session on a
    table AND releases any already-held orders), this only flips the
    `verified` flag on ONE session row — called once per diner session
    (host or participant) right after it's created/linked.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE table_sessions SET verified = true WHERE id = $1",
            session_id,
        )


async def db_mark_table_ghost(table_id: str, org_id: int, mesero_username: str) -> dict:
    """Waiter marks this table as a ghost (no real customer).

    1. Cancels all pending_table_validation orders on this table.
    2. Closes active sessions (status='ghost_blocked').
    3. Returns the list of phone numbers that had sessions (for blocklist).

    Returns: {"orders_cancelled": N, "phones": [str, ...]}

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # 1. Cancel pending orders
        cancelled = await conn.fetchval(
            """
            WITH updated AS (
                UPDATE table_orders
                SET    status = 'cancelado',
                       updated_at = NOW()
                WHERE  table_id = $1
                  AND  pending_table_validation = true
                  AND  status NOT IN ('cancelado', 'entregado', 'factura_entregada')
                RETURNING id
            )
            SELECT COUNT(*) FROM updated
            """,
            table_id,
        )
        # 2. Collect phones and close sessions
        phones_rows = await conn.fetch(
            """
            UPDATE table_sessions
            SET    status = 'ghost_blocked'
            WHERE  table_id = $1
              AND  status = 'active'
            RETURNING phone
            """,
            table_id,
        )
        phones = list({r["phone"] for r in phones_rows})

    from app.services.logging import get_logger as _gl
    _log = _gl("tables_repo")
    _log.info(
        "table.ghost_marked",
        table_id=table_id,
        org_id=org_id,
        marked_by=mesero_username,
        orders_cancelled=int(cancelled or 0),
        phones_blocked=len(phones),
    )
    return {"orders_cancelled": int(cancelled or 0), "phones": phones}
