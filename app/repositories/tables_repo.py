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
  - Kitchen/delivery views (db_get_delivery_orders_for_caja,
    db_get_delivery_status_hash) are cross-tenant kitchen displays.
  - db_get_waiter_alerts(bot_number) is tenant-scoped; call site must pass bot_number.
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


async def db_get_tables(branch_id: int = None, is_main: bool = False):
    """
    Devuelve las mesas.
    Si branch_id tiene un número, trae las de esa sucursal/matriz.
    Si branch_id es None, trae TODAS las mesas (admin global).
    (is_main ya no se usa — branch_id siempre es NOT NULL tras migración 0018)

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number", branch_id)
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


async def db_auto_create_table(restaurant_id: int, is_main_restaurant: bool) -> dict:
    """
    Crea una mesa automáticamente buscando el primer número disponible.
    El nombre será {restaurant_id}-{numero}. (Ej. "1-1", "2-1").
    Reutiliza automáticamente los números de las mesas que hayan sido borradas.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    # branch_id is always the restaurant_id (parent or branch)
    branch_id = restaurant_id

    async with tenant_connection() as conn:
        # 1. Obtener todos los números actualmente en uso (ignorando los borrados)
        rows = await conn.fetch("SELECT number FROM restaurant_tables WHERE branch_id=$1 AND active=TRUE", branch_id)

        used_numbers = {r["number"] for r in rows}

        # 2. Buscar el primer "hueco" disponible (si se borró la 2, la próxima será la 2)
        new_number = 1
        while new_number in used_numbers:
            new_number += 1

        # 3. Armar el nombre limpio que verá el usuario y el bot
        table_name = f"{restaurant_id}-{new_number}"
        table_id = f"table-{restaurant_id}-{new_number}"

        # 4. Insertar o reactivar si el ID ya existía en la base de datos
        # Wave-2: same shape as db_create_table — must populate org_id (from GUC)
        # and location_id (mirror of branch_id) explicitly. See db_create_table.
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, location_id, org_id, active)
            VALUES ($1, $2, $3, $4, $4,
                    NULLIF(current_setting('app.org_id', true), '')::bigint,
                    TRUE)
            ON CONFLICT (id) DO UPDATE SET active=TRUE, name=EXCLUDED.name, number=EXCLUDED.number
        """, table_id, new_number, table_name, branch_id)

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
        await conn.execute("""
            INSERT INTO table_orders
                (id, table_id, table_name, phone, items, status, notes, total,
                 base_order_id, sub_number, station, branch_id, org_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items,
                status=EXCLUDED.status,
                notes=EXCLUDED.notes,
                total=EXCLUDED.total,
                branch_id=EXCLUDED.branch_id,
                updated_at=NOW()
        """, order['id'], order['table_id'], order['table_name'], order['phone'],
            json.dumps(order['items']),
            order.get('status', 'recibido'),
            order.get('notes', ''),
            order.get('total', 0),
            order.get('base_order_id'),
            order.get('sub_number', 1),
            order.get('station', 'all'),
            order.get('branch_id'),
            rid)


async def db_get_base_order_status(base_order_id: str) -> str | None:
    """Returns the status of the base order record itself (not sub-orders).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM table_orders WHERE id=$1", base_order_id
        )
        return row["status"] if row else None


async def db_merge_table_order_items(base_order_id: str, new_items: list, additional_total: float) -> bool:
    """Merges new items into the base order when it's still in 'recibido' status.
    Combines quantities for duplicate item names. Returns False if order is no longer recibido.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT items, total FROM table_orders WHERE id=$1 AND status='recibido'",
            base_order_id
        )
        if row is None:
            return False

        existing = row["items"]
        if isinstance(existing, str):
            try: existing = json.loads(existing)
            except: existing = []
        existing = existing or []

        items_map = {i["name"]: dict(i) for i in existing}
        for ni in new_items:
            name = ni["name"]
            if name in items_map:
                items_map[name]["quantity"] = items_map[name].get("quantity", 1) + ni.get("quantity", 1)
            else:
                items_map[name] = dict(ni)

        merged = list(items_map.values())
        new_total = to_decimal(row["total"]) + to_decimal(additional_total)
        await conn.execute(
            "UPDATE table_orders SET items=$2, total=$3, updated_at=NOW() WHERE id=$1",
            base_order_id, json.dumps(merged), new_total
        )
        return True


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
        await conn.execute("UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1", order_id, status)


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


async def db_mark_factura_generada(base_order_id: str) -> None:
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
    Retorna los ítems y total agregados de todas las sub-órdenes de un ticket.
    Usado por create_checks para validar cantidades antes de crear la división.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            rows = await conn.fetch(
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
    }


# ── waiter_alerts ─────────────────────────────────────────────────────────────

async def db_init_waiter_alerts():
    """No-op: waiter_alerts managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_create_waiter_alert(phone: str, bot_number: str, alert_type: str, message: str, table_id: str = "", table_name: str = "") -> dict:
    """
    Create a waiter alert. For non-billing alerts (alert_type='waiter'), if an
    open alert already exists for the same table within the last 60 seconds,
    append the new message to the existing alert instead of creating a duplicate.
    This prevents the waiter from receiving multiple pings when the customer
    mentions the same request across two consecutive turns.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # Dedup window: 60 seconds for waiter (non-bill) alerts only
        if alert_type == "waiter" and table_id:
            existing = await conn.fetchrow(
                """
                SELECT id, message FROM waiter_alerts
                WHERE table_id = $1
                  AND bot_number = $2
                  AND alert_type = 'waiter'
                  AND dismissed = FALSE
                  AND created_at > NOW() - INTERVAL '60 seconds'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                table_id, bot_number,
            )
            if existing:
                combined = f"{existing['message']} | {message}"
                row = await conn.fetchrow(
                    "UPDATE waiter_alerts SET message = $1 WHERE id = $2 RETURNING *",
                    combined, existing["id"],
                )
                return _serialize(dict(row))

        row = await conn.fetchrow(
            "INSERT INTO waiter_alerts (table_id, table_name, phone, bot_number, alert_type, message, org_id) "
            "VALUES ($1, $2, $3, $4, $5, $6, NULLIF(current_setting('app.org_id', true), '')::bigint) RETURNING *",
            table_id, table_name, phone, bot_number, alert_type, message,
        )
        return _serialize(dict(row))


async def db_get_waiter_alerts(bot_number: str) -> list:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        rows = await conn.fetch("SELECT * FROM waiter_alerts WHERE bot_number=$1 AND dismissed=FALSE AND created_at > NOW() - INTERVAL '2 hours' ORDER BY created_at DESC", bot_number)
        return [_serialize(dict(r)) for r in rows]


async def db_dismiss_waiter_alert(alert_id: int) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        result = await conn.execute("UPDATE waiter_alerts SET dismissed=TRUE WHERE id=$1", alert_id)
        return result == "UPDATE 1"


# ── table_sessions ────────────────────────────────────────────────────────────

async def db_init_table_sessions():
    """No-op: table_sessions managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_get_active_session(phone: str, bot_number: str) -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND bot_number=$2 AND status='active' ORDER BY started_at DESC LIMIT 1", phone, bot_number)
        return _serialize(dict(row)) if row else None


async def db_create_table_session(
    phone: str,
    bot_number: str,
    table_id: str,
    table_name: str,
    org_id: int | None = None,
    location_id: int | None = None,
) -> dict:
    """# Requires active tenant_scope() or bypass_tenant_scope().

    org_id is required by schema (NOT NULL). If not passed, resolved from
    restaurant_tables.org_id. location_id is also persisted when available.
    """
    async with tenant_connection() as conn:
        if org_id is None or location_id is None:
            tbl = await conn.fetchrow(
                "SELECT org_id, location_id FROM restaurant_tables WHERE id=$1",
                table_id,
            )
            if not tbl:
                raise ValueError(f"Cannot resolve org for table {table_id}")
            if org_id is None:
                org_id = tbl["org_id"]
            if location_id is None:
                location_id = tbl["location_id"]
        if org_id is None:
            raise ValueError(f"Cannot resolve org_id for table {table_id}")
        row = await conn.fetchrow(
            "INSERT INTO table_sessions (phone, bot_number, table_id, table_name, org_id, location_id, status, last_activity) "
            "VALUES ($1, $2, $3, $4, $5, $6, 'active', NOW()) RETURNING *",
            phone, bot_number, table_id, table_name, org_id, location_id,
        )
        return _serialize(dict(row))


async def db_touch_session(phone: str, bot_number: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)


async def db_touch_session_with_phone_id(phone: str, bot_number: str, meta_phone_id: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW(), meta_phone_id=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, meta_phone_id)


async def db_session_mark_order(phone: str, bot_number: str):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET has_order=TRUE, last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)


async def db_session_mark_delivered(phone: str, bot_number: str, total: int = 0):
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute("UPDATE table_sessions SET order_delivered=TRUE, last_activity=NOW(), total_spent=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, total)


async def db_mark_session_nps_pending(phone: str, bot_number: str) -> None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        await conn.execute(
            "UPDATE table_sessions SET status='nps_pending', closed_by='factura_entregada', last_activity=NOW() "
            "WHERE phone=$1 AND bot_number=$2 AND status='active'",
            phone, bot_number
        )


async def db_close_session(phone: str, bot_number: str, reason: str = "manual", closed_by_username: str = "") -> dict | None:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        row = await conn.fetchrow("""
            UPDATE table_sessions
            SET status='closed', closed_at=NOW(), closed_by=$3, closed_by_username=$4,
                summary=jsonb_build_object('close_reason',$3::text,'closed_by_user',$4::text)
            WHERE phone=$1 AND bot_number=$2 AND status IN ('active','nps_pending') RETURNING *
        """, phone, bot_number, reason, closed_by_username)
        return _serialize(dict(row)) if row else None


async def db_mark_session_warned(session_id: int) -> bool:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    async with tenant_connection() as conn:
        # El AND inactivity_warned=FALSE asegura que solo 1 worker pueda hacer el UPDATE
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


async def db_get_closed_sessions(bot_number: str, hours: int = 24) -> list:
    """# Requires active tenant_scope() or bypass_tenant_scope()."""
    hours = max(1, min(hours, 720))
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            "SELECT * FROM table_sessions WHERE bot_number=$1 AND status='closed'"
            " AND closed_at > NOW() - make_interval(hours => $2) ORDER BY closed_at DESC",
            bot_number, hours,
        )
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
        bot_number = target["bot_number"]
        await conn.execute("UPDATE table_sessions SET status='closed', closed_at=NOW(), closed_by='superseded', closed_by_username='' WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)
        row = await conn.fetchrow("UPDATE table_sessions SET status='active', closed_at=NULL, closed_by='', closed_by_username='', inactivity_warned=FALSE, last_activity=NOW(), summary=jsonb_build_object('reopened',true) WHERE id=$1 RETURNING *", session_id)
        return _serialize(dict(row)) if row else None


# ── table_checks ──────────────────────────────────────────────────────────────

async def db_init_table_checks():
    """No-op: table_checks managed by Alembic (0001_initial_schema.py)."""
    pass


async def db_create_checks(base_order_id: str, checks: list) -> list:
    """
    Reemplaza los checks 'open' del ticket y crea los nuevos.
    checks = [{"check_number": 1, "items": [...], "subtotal": N, "tax_amount": N, "total": N}, ...]
    Cada item en items: {"name": str, "qty": int, "unit_price": float, "subtotal": float}

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # tenant_connection() opens a transaction; conn.transaction() creates a SAVEPOINT.
        async with conn.transaction():
            # Eliminar únicamente los checks todavía abiertos (no los ya cobrados)
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
    return [_serialize(dict(r)) for r in rows]


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
) -> None:
    """
    Atómicamente:
    1. Actualiza el check a status='invoiced' con pagos y cambio.
    2. Si TODOS los checks del base_order_id están en {invoiced, cancelled},
       actualiza table_orders a status='factura_entregada'.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        # tenant_connection() opens a transaction; conn.transaction() creates a SAVEPOINT.
        async with conn.transaction():
            await conn.execute(
                """UPDATE table_checks
                   SET payments=$1::jsonb, change_amount=$2,
                       fiscal_invoice_id=$3, status='invoiced',
                       customer_name=$4, customer_nit=$5, customer_email=$6,
                       tip_amount=$7, paid_at=NOW()
                   WHERE id=$8""",
                json.dumps(payments), to_decimal(change_amount),
                fiscal_invoice_id,
                customer_name, customer_nit, customer_email,
                to_decimal(tip_amount), check_id
            )
            # Marcar propuesta como confirmada si existía
            await conn.execute(
                """UPDATE table_checks
                   SET proposal_status = 'confirmed'
                 WHERE id = $1 AND proposal_status IS NOT NULL""",
                check_id
            )
            # Verificar si todos los checks del grupo están cerrados
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


async def db_delete_open_check(check_id: str) -> bool:
    """Elimina un check solo si está en estado 'open'. Retorna True si se eliminó.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        result = await conn.execute(
            "DELETE FROM table_checks WHERE id=$1 AND status='open'", check_id
        )
    return result != "DELETE 0"


async def db_attach_proposal(
    check_id: str,
    proposed_payments: list,
    proposed_tip: float,
    proposal_source: str,
    proposal_status: str,
    customer_phone: str,
) -> None:
    """Adjunta metadatos de propuesta de pago a un check existente.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            """UPDATE table_checks
               SET proposed_payments     = $2::jsonb,
                   proposed_tip          = $3,
                   proposal_source       = $4,
                   proposal_status       = $5,
                   proposal_customer_phone = $6,
                   proposal_created_at   = NOW()
             WHERE id = $1""",
            check_id,
            json.dumps(proposed_payments),
            to_decimal(proposed_tip),
            proposal_source,
            proposal_status,
            customer_phone,
        )


async def db_set_check_tip(check_id: str, tip_amount: float) -> None:
    """Actualiza tip_amount en un check abierto (durante el flujo de checkout del bot).

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
    Adjunta URL de comprobante a los checks con propuesta awaiting_proof del cliente.
    Retorna True si se actualizó al menos un check.

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
    Busca si el cliente tiene algún check con propuesta pendiente/esperando comprobante.
    Útil en chat.py para interceptar imágenes y adjuntarlas sin pasar por el LLM.
    Retorna el check (con base_order_id) o None.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            """SELECT tc.id, tc.base_order_id, tc.proposal_status, tc.proposed_payments
               FROM table_checks tc
               JOIN table_orders tor ON tor.base_order_id = tc.base_order_id
              WHERE tc.proposal_customer_phone = $2
                AND tc.proposal_status IN ('pending', 'awaiting_proof')
                AND tor.branch_id = $1
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
    Lista mesas que tienen checks con propuestas bot activas (pending/awaiting_proof/proof_received).
    Agrupado por base_order_id para la vista de Caja.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    ids = branch_ids if branch_ids else [restaurant_id]
    async with tenant_connection() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (tor.table_name, COALESCE(tor.branch_id, 0))
                 tor.base_order_id,
                 tor.table_name,
                 tor.total           AS order_total,
                 tor.branch_id,
                 json_agg(tc.* ORDER BY tc.check_number) AS checks
               FROM table_orders tor
               JOIN table_checks tc ON tc.base_order_id = tor.base_order_id
              WHERE tor.branch_id = ANY($1::int[])
                AND tc.proposal_status IN ('pending', 'awaiting_proof', 'proof_received')
                AND tc.status = 'open'
              GROUP BY tor.base_order_id, tor.table_name, tor.total, tor.branch_id
              ORDER BY tor.table_name, COALESCE(tor.branch_id, 0),
                       MIN(tc.proposal_created_at) DESC""",
            ids,
        )
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
    """Devuelve datos del check + info fiscal para impresión de factura.

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
    allowed = {"capacity", "table_type", "zone"}
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


async def db_get_floor_plan(branch_id: int = None, is_main: bool = False):
    """Get all tables with positions and current occupancy for floor plan view.

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
            sql += " AND t.branch_id = $1"
            params.append(branch_id)
        sql += " ORDER BY t.zone, t.number"
        rows = await conn.fetch(sql, *params)
        return [_serialize(dict(r)) for r in rows]


async def db_get_session_phones_by_branch(branch_id: int, bot_number: str) -> set:
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
            WHERE rt.branch_id = $1 AND ts.bot_number = $2
            """,
            branch_id, bot_number,
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
            "SELECT table_id, status FROM table_orders "
            "WHERE status NOT IN ('factura_entregada', 'cancelado') AND branch_id = $1",
            branch_id,
        )
    return [dict(r) for r in rows]


async def db_dismiss_waiter_alert(alert_id: int) -> None:
    """Delete a waiter alert by id.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute("DELETE FROM waiter_alerts WHERE id = $1", alert_id)


async def db_get_table_orders_for_branch(
    branch_id: int | None,
    status: str | None = None,
    is_admin: bool = False,
) -> list:
    """
    Return table orders filtered by branch and optional status.
    If branch_id is None and is_admin=True, returns all orders.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if branch_id is not None:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status = $1 AND branch_id = $2 ORDER BY created_at ASC",
                    status, branch_id,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') AND branch_id = $1 ORDER BY created_at ASC",
                    branch_id,
                )
        elif is_admin:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status = $1 ORDER BY created_at ASC",
                    status,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM table_orders WHERE status NOT IN ('factura_entregada','cancelado') ORDER BY created_at ASC"
                )
        else:
            rows = []
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
    """Return phone, table_name, base_order_id, table_id for a table order.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT phone, table_name, base_order_id, table_id FROM table_orders WHERE id=$1",
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
            "SELECT id FROM restaurants WHERE id = $1 AND parent_restaurant_id = $2",
            branch_id, parent_id,
        )
    return row is not None


async def db_get_delivery_orders_for_caja() -> list:
    """
    Return pending delivery/pickup orders for the kitchen/caja view (last 24h,
    excluding terminal statuses).

    Cross-tenant kitchen display.
    # Uses bypass_tenant_scope internally (global kitchen view).
    """
    with bypass_tenant_scope("kitchen: delivery orders for caja cross-tenant view"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                """SELECT * FROM orders
                   WHERE order_type IN ('domicilio','recoger')
                   AND created_at >= NOW() - INTERVAL '24 hours'
                   AND status NOT IN ('en_camino', 'en_puerta', 'entregado', 'cancelado')
                   ORDER BY created_at DESC"""
            )
    return [dict(r) for r in rows]


async def db_get_delivery_status_hash() -> list:
    """Return id+status pairs for active delivery orders (for hash-based polling).

    Cross-tenant kitchen display.
    # Uses bypass_tenant_scope internally (global kitchen view).
    """
    with bypass_tenant_scope("kitchen: delivery status hash cross-tenant polling"):
        async with tenant_connection() as conn:
            rows = await conn.fetch(
                "SELECT id, status FROM orders WHERE order_type IN ('domicilio','recoger')"
                " AND created_at >= NOW() - INTERVAL '24 hours' ORDER BY created_at DESC"
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
            JOIN restaurants r ON r.whatsapp_number = o.bot_number
            WHERE o.order_type IN ('domicilio', 'recoger')
              AND o.status IN ('pendiente', 'confirmado', 'en_preparacion', 'listo', 'en_camino', 'en_puerta')
              AND r.id = $1
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


async def db_get_delivery_order_contact(order_id: str) -> dict | None:
    """Return phone, address, total for a delivery order (for WA notification).

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT phone, address, total FROM orders WHERE id=$1", order_id
        )
    return dict(row) if row else None


async def db_get_meta_phone_id_for_session(phone: str) -> str | None:
    """Return the most recent meta_phone_id from a table session for a phone.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        row = await conn.fetchrow(
            "SELECT meta_phone_id FROM table_sessions WHERE phone=$1 ORDER BY started_at DESC LIMIT 1",
            phone,
        )
    return row["meta_phone_id"] if row else None


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


async def db_insert_session_waiter_alert(
    table_id: str, table_name: str, message: str
) -> None:
    """Insert a waiter alert triggered from a dashboard session alert.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        await conn.execute(
            "INSERT INTO waiter_alerts (table_id, table_name, message, status, org_id) "
            "VALUES ($1, $2, $3, 'active', NULLIF(current_setting('app.org_id', true), '')::bigint)",
            table_id, table_name, message,
        )


async def db_get_closed_sessions(hours: int, bot_number: str | None) -> list[dict]:
    """Return closed table sessions within the given hours window.

    # Requires active tenant_scope() or bypass_tenant_scope().
    """
    async with tenant_connection() as conn:
        if bot_number:
            rows = await conn.fetch(
                "SELECT * FROM table_sessions WHERE closed_at IS NOT NULL"
                " AND closed_at >= NOW() - ($1 * INTERVAL '1 hour')"
                " AND bot_number = $2 ORDER BY closed_at DESC",
                hours, bot_number,
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
