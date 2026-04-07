"""
Tables / POS repository — Fase 6 extraction from app.services.database.

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
"""

from __future__ import annotations

import json

from app.services.money import to_decimal, ZERO


# Lazy accessors — break circular import with app.services.database.
# database.py re-exports this module at module level, so a top-level import
# of database here would create a cycle. We resolve both helpers at call time.

async def _get_pool():
    from app.services.database import get_pool  # noqa: PLC0415
    return await get_pool()

def _serialize(d: dict) -> dict:
    from app.services.database import _serialize as _db_serialize  # noqa: PLC0415
    return _db_serialize(d)


# ── restaurant_tables ────────────────────────────────────────────────────────

async def db_init_tables():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS restaurant_tables (
                id TEXT PRIMARY KEY,
                number INTEGER NOT NULL,
                name TEXT NOT NULL,
                branch_id INTEGER,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS table_orders (
                id TEXT PRIMARY KEY,
                table_id TEXT NOT NULL,
                table_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                items JSONB NOT NULL DEFAULT '[]',
                status TEXT DEFAULT 'recibido',
                notes TEXT DEFAULT '',
                total INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        for col_sql in [
            "ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER",
            "ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS base_order_id TEXT DEFAULT NULL",
            "ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS sub_number INTEGER DEFAULT 1",
            # FASE 2: enrutamiento multi-estación Cocina / Bar
            # DEFAULT 'all' → pedidos existentes siguen apareciendo en todos los KDS
            "ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS station TEXT NOT NULL DEFAULT 'all'",
        ]:
            try: await conn.execute(col_sql)
            except Exception: pass
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_table_orders_base    ON table_orders(base_order_id)",
            "CREATE INDEX IF NOT EXISTS idx_table_orders_station ON table_orders(station)",
        ]:
            try: await conn.execute(idx_sql)
            except Exception: pass


async def db_get_tables(branch_id: int = None, is_main: bool = False):
    """
    Devuelve las mesas.
    Si is_main es True, trae SOLO las mesas de la matriz (branch_id IS NULL).
    Si branch_id tiene un número, trae SOLO las de esa sucursal.
    Si ambas son False/None, trae TODAS las mesas.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        if is_main:
            # Es la Matriz. Obligamos a buscar branch_id IS NULL
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id IS NULL ORDER BY number")
        elif branch_id is not None:
            # Es una sucursal. Buscamos su ID específico
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number", branch_id)
        else:
            # Modo Admin Global: Trae todo
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")

        return [_serialize(dict(r)) for r in rows]


async def db_create_table(table_id: str, number: int, name: str, branch_id: int = None):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, active)
            VALUES ($1,$2,$3,$4,TRUE)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name, branch_id=EXCLUDED.branch_id, active=TRUE
        """, table_id, number, name, branch_id)


async def db_auto_create_table(restaurant_id: int, is_main_restaurant: bool) -> dict:
    """
    Crea una mesa automáticamente buscando el primer número disponible.
    El nombre será {restaurant_id}-{numero}. (Ej. "1-1", "2-1").
    Reutiliza automáticamente los números de las mesas que hayan sido borradas.
    """
    branch_id = None if is_main_restaurant else restaurant_id

    pool = await _get_pool()
    async with pool.acquire() as conn:
        # 1. Obtener todos los números actualmente en uso (ignorando los borrados)
        if branch_id is None:
            rows = await conn.fetch("SELECT number FROM restaurant_tables WHERE branch_id IS NULL AND active=TRUE")
        else:
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
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, active)
            VALUES ($1, $2, $3, $4, TRUE)
            ON CONFLICT (id) DO UPDATE SET active=TRUE, name=EXCLUDED.name, number=EXCLUDED.number
        """, table_id, new_number, table_name, branch_id)

        return {
            "id": table_id,
            "number": new_number,
            "name": table_name,
            "branch_id": branch_id
        }


async def db_delete_table(table_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE restaurant_tables SET active=FALSE WHERE id=$1", table_id)


async def db_get_table_by_id(table_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurant_tables WHERE id=$1", table_id)
        return _serialize(dict(row)) if row else None


# ── table_orders ─────────────────────────────────────────────────────────────

async def db_save_table_order(order: dict):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO table_orders
                (id, table_id, table_name, phone, items, status, notes, total,
                 base_order_id, sub_number, station, branch_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
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
            order.get('branch_id')) # 🛡️ Parámetro $12: Grabado con éxito


async def db_get_base_order_status(base_order_id: str) -> str | None:
    """Returns the status of the base order record itself (not sub-orders)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM table_orders WHERE id=$1", base_order_id
        )
        return row["status"] if row else None


async def db_merge_table_order_items(base_order_id: str, new_items: list, additional_total: float) -> bool:
    """Merges new items into the base order when it's still in 'recibido' status.
    Combines quantities for duplicate item names. Returns False if order is no longer recibido."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1", order_id, status)


async def db_get_base_order_id(table_id: str) -> str | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(base_order_id, id) as base_id
            FROM table_orders
            WHERE table_id=$1 AND status NOT IN ('factura_entregada', 'cancelado')
            ORDER BY created_at ASC LIMIT 1
        """, table_id)
        return row['base_id'] if row else None


async def db_get_next_sub_number(base_order_id: str) -> int:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT MAX(sub_number) as max_sub FROM table_orders WHERE base_order_id=$1 OR id=$1", base_order_id)
        return (row['max_sub'] or 0) + 1


async def db_get_table_bill(base_order_id: str) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE table_orders SET status='factura_entregada', updated_at=NOW() WHERE (base_order_id=$1 OR id=$1) AND status NOT IN ('cancelado')", base_order_id)
        return result != "UPDATE 0"


async def db_mark_factura_generada(base_order_id: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE table_orders SET status='factura_generada', updated_at=NOW() "
            "WHERE (id=$1 OR base_order_id=$1) AND status NOT IN ('cancelado','factura_entregada')",
            base_order_id
        )


async def db_get_first_table_order(base_order_id: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT phone, table_id, status FROM table_orders "
            "WHERE (id=$1 OR base_order_id=$1) ORDER BY created_at ASC LIMIT 1",
            base_order_id
        )
    return _serialize(dict(row)) if row else None


async def db_cleanup_after_checkout(phone: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)
        await conn.execute("DELETE FROM carts WHERE phone=$1", phone)


async def db_get_open_session_by_phone(phone: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL ORDER BY started_at DESC LIMIT 1",
            phone
        )
    return _serialize(dict(row)) if row else None


async def db_has_pending_invoice(phone: str) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM table_orders WHERE phone=$1 AND status='entregado' LIMIT 1", phone)
        return row is not None


async def db_get_active_table_order(phone: str, table_id: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS waiter_alerts (
                id         SERIAL PRIMARY KEY,
                table_id   TEXT    NOT NULL DEFAULT '',
                table_name TEXT    NOT NULL DEFAULT '',
                phone      TEXT    NOT NULL,
                bot_number TEXT    NOT NULL DEFAULT '',
                alert_type TEXT    NOT NULL DEFAULT 'waiter',
                message    TEXT    NOT NULL DEFAULT '',
                dismissed  BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)


async def db_create_waiter_alert(phone: str, bot_number: str, alert_type: str, message: str, table_id: str = "", table_name: str = "") -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO waiter_alerts (table_id, table_name, phone, bot_number, alert_type, message) VALUES ($1, $2, $3, $4, $5, $6) RETURNING *", table_id, table_name, phone, bot_number, alert_type, message)
        return _serialize(dict(row))


async def db_get_waiter_alerts(bot_number: str) -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM waiter_alerts WHERE bot_number=$1 AND dismissed=FALSE AND created_at > NOW() - INTERVAL '2 hours' ORDER BY created_at DESC", bot_number)
        return [_serialize(dict(r)) for r in rows]


async def db_dismiss_waiter_alert(alert_id: int) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE waiter_alerts SET dismissed=TRUE WHERE id=$1", alert_id)
        return result == "UPDATE 1"


# ── table_sessions ────────────────────────────────────────────────────────────

async def db_init_table_sessions():
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS table_sessions (
                id                 SERIAL PRIMARY KEY,
                table_id           TEXT    NOT NULL DEFAULT '',
                table_name         TEXT    NOT NULL DEFAULT '',
                phone              TEXT    NOT NULL,
                bot_number         TEXT    NOT NULL DEFAULT '',
                status             TEXT    NOT NULL DEFAULT 'active',
                has_order          BOOLEAN DEFAULT FALSE,
                order_delivered    BOOLEAN DEFAULT FALSE,
                inactivity_warned  BOOLEAN DEFAULT FALSE,
                last_activity      TIMESTAMP DEFAULT NOW(),
                started_at         TIMESTAMP DEFAULT NOW(),
                closed_at          TIMESTAMP,
                total_spent        INTEGER DEFAULT 0,
                closed_by          TEXT    DEFAULT '',
                closed_by_username TEXT    DEFAULT '',
                summary            JSONB   DEFAULT '{}'::jsonb
            );
            CREATE INDEX IF NOT EXISTS idx_table_sessions_active ON table_sessions (phone, bot_number, status);
            CREATE INDEX IF NOT EXISTS idx_table_sessions_closed ON table_sessions (bot_number, closed_at DESC);
        """)
        for col_sql in [
            "ALTER TABLE table_sessions ADD COLUMN IF NOT EXISTS closed_by TEXT DEFAULT ''",
            "ALTER TABLE table_sessions ADD COLUMN IF NOT EXISTS closed_by_username TEXT DEFAULT ''",
            "ALTER TABLE table_sessions ADD COLUMN IF NOT EXISTS meta_phone_id TEXT DEFAULT ''",
        ]:
            try: await conn.execute(col_sql)
            except Exception: pass


async def db_get_active_session(phone: str, bot_number: str) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND bot_number=$2 AND status='active' ORDER BY started_at DESC LIMIT 1", phone, bot_number)
        return _serialize(dict(row)) if row else None


async def db_create_table_session(phone: str, bot_number: str, table_id: str, table_name: str) -> dict:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO table_sessions (phone, bot_number, table_id, table_name, status, last_activity) VALUES ($1, $2, $3, $4, 'active', NOW()) RETURNING *", phone, bot_number, table_id, table_name)
        return _serialize(dict(row))


async def db_touch_session(phone: str, bot_number: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)


async def db_touch_session_with_phone_id(phone: str, bot_number: str, meta_phone_id: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW(), meta_phone_id=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, meta_phone_id)


async def db_session_mark_order(phone: str, bot_number: str):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET has_order=TRUE, last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)


async def db_session_mark_delivered(phone: str, bot_number: str, total: int = 0):
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET order_delivered=TRUE, last_activity=NOW(), total_spent=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, total)


async def db_mark_session_nps_pending(phone: str, bot_number: str) -> None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE table_sessions SET status='nps_pending', closed_by='factura_entregada', last_activity=NOW() "
            "WHERE phone=$1 AND bot_number=$2 AND status='active'",
            phone, bot_number
        )


async def db_close_session(phone: str, bot_number: str, reason: str = "manual", closed_by_username: str = "") -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE table_sessions
            SET status='closed', closed_at=NOW(), closed_by=$3, closed_by_username=$4,
                summary=jsonb_build_object('close_reason',$3::text,'closed_by_user',$4::text)
            WHERE phone=$1 AND bot_number=$2 AND status IN ('active','nps_pending') RETURNING *
        """, phone, bot_number, reason, closed_by_username)
        return _serialize(dict(row)) if row else None


async def db_mark_session_warned(session_id: int) -> bool:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        # El AND inactivity_warned=FALSE asegura que solo 1 worker pueda hacer el UPDATE
        result = await conn.execute(
            "UPDATE table_sessions SET inactivity_warned=TRUE WHERE id=$1 AND inactivity_warned=FALSE",
            session_id
        )
        return result == "UPDATE 1"


async def db_get_stale_sessions() -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM table_sessions WHERE status='active' AND inactivity_warned=FALSE
            AND ((has_order=FALSE AND last_activity < NOW() - INTERVAL '10 minutes')
              OR (order_delivered=TRUE AND last_activity < NOW() - INTERVAL '60 minutes'))
        """)
        return [_serialize(dict(r)) for r in rows]


async def db_get_closeable_sessions() -> list:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM table_sessions WHERE
            (status='active' AND inactivity_warned=TRUE AND last_activity < NOW() - INTERVAL '5 minutes')
            OR (status='nps_pending' AND last_activity < NOW() - INTERVAL '5 minutes')
        """)
        return [_serialize(dict(r)) for r in rows]


async def db_get_closed_sessions(bot_number: str, hours: int = 24) -> list:
    hours = max(1, min(hours, 720))
    pool = await _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM table_sessions WHERE bot_number=$1 AND status='closed'"
            " AND closed_at > NOW() - ($2 * INTERVAL '1 hour') ORDER BY closed_at DESC",
            bot_number, hours,
        )
        return [_serialize(dict(r)) for r in rows]


async def db_get_session_by_id(session_id: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1", session_id)
        return _serialize(dict(row)) if row else None


async def db_reopen_session(session_id: int) -> dict | None:
    pool = await _get_pool()
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1 AND status='closed'", session_id)
        if not target: return None
        phone = target["phone"]
        bot_number = target["bot_number"]
        await conn.execute("UPDATE table_sessions SET status='closed', closed_at=NOW(), closed_by='superseded', closed_by_username='' WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)
        row = await conn.fetchrow("UPDATE table_sessions SET status='active', closed_at=NULL, closed_by='', closed_by_username='', inactivity_warned=FALSE, last_activity=NOW(), summary=jsonb_build_object('reopened',true) WHERE id=$1 RETURNING *", session_id)
        return _serialize(dict(row)) if row else None


# ── table_checks ──────────────────────────────────────────────────────────────

async def db_init_table_checks():
    """
    Crea la tabla table_checks para división de cuentas y pagos mixtos.
    Cada check es una unidad de cobro independiente con su propia factura DIAN.
    Llamar desde main.py en el startup.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS table_checks (
                id                TEXT PRIMARY KEY,
                base_order_id     TEXT NOT NULL,
                check_number      SMALLINT NOT NULL,
                items             JSONB NOT NULL DEFAULT '[]',
                subtotal          NUMERIC(10,2) NOT NULL DEFAULT 0,
                tax_amount        NUMERIC(10,2) NOT NULL DEFAULT 0,
                total             NUMERIC(10,2) NOT NULL DEFAULT 0,
                payments          JSONB NOT NULL DEFAULT '[]',
                change_amount     NUMERIC(10,2) NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'open',
                fiscal_invoice_id INTEGER REFERENCES fiscal_invoices(id),
                customer_name     TEXT,
                customer_nit      TEXT,
                customer_email    TEXT,
                created_at        TIMESTAMP DEFAULT NOW(),
                paid_at           TIMESTAMP,
                UNIQUE (base_order_id, check_number)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_table_checks_base "
            "ON table_checks(base_order_id)"
        )
    print("Table checks table ready", flush=True)


async def db_create_checks(base_order_id: str, checks: list) -> list:
    """
    Reemplaza los checks 'open' del ticket y crea los nuevos.
    checks = [{"check_number": 1, "items": [...], "subtotal": N, "tax_amount": N, "total": N}, ...]
    Cada item en items: {"name": str, "qty": int, "unit_price": float, "subtotal": float}
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """Devuelve todos los checks de un ticket, con datos fiscales si existen."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """Devuelve un check individual."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """Elimina un check solo si está en estado 'open'. Retorna True si se eliminó."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """Adjunta metadatos de propuesta de pago a un check existente."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """Actualiza tip_amount en un check abierto (durante el flujo de checkout del bot)."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE table_checks SET tip_amount = $1 WHERE id = $2",
            to_decimal(tip_amount),
            check_id,
        )


async def db_attach_proof(base_order_id: str, customer_phone: str, media_url: str) -> bool:
    """
    Adjunta URL de comprobante a los checks con propuesta awaiting_proof del cliente.
    Retorna True si se actualizó al menos un check.
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
    """
    pool = await _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT tc.id, tc.base_order_id, tc.proposal_status, tc.proposed_payments
               FROM table_checks tc
               JOIN table_orders tor ON tor.base_order_id = tc.base_order_id
              WHERE tc.proposal_customer_phone = $2
                AND tc.proposal_status IN ('pending', 'awaiting_proof')
                AND tor.restaurant_id = $1
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
    """
    pool = await _get_pool()
    ids = branch_ids if branch_ids else []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT
                 tor.base_order_id,
                 tor.table_name,
                 tor.total           AS order_total,
                 tor.restaurant_id,
                 json_agg(tc.* ORDER BY tc.check_number) AS checks
               FROM table_orders tor
               JOIN table_checks tc ON tc.base_order_id = tor.base_order_id
              WHERE tor.restaurant_id = $1
                AND tc.proposal_status IN ('pending', 'awaiting_proof', 'proof_received')
                AND tc.status = 'open'
              GROUP BY tor.base_order_id, tor.table_name, tor.total, tor.restaurant_id
              ORDER BY MIN(tc.proposal_created_at) ASC""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_check_ticket(check_id: str) -> dict | None:
    """Devuelve datos del check + info fiscal para impresión de factura."""
    pool = await _get_pool()
    async with pool.acquire() as conn:
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
