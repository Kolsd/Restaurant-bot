import os
import asyncpg
import json
from datetime import date, datetime, timedelta

_pool = None

SESSION_TTL_HOURS = 72  # V-06: tokens expiran en 72 horas

def _normalize_phone(number: str) -> str:
    if not number: return ""
    return number.replace(" ", "").replace("+", "")

def _to_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def _serialize(d: dict) -> dict:
    result = {}
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()[:19]
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    return result

async def get_pool():
    global _pool
    if _pool is None:
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise RuntimeError("DATABASE_URL no esta configurada")
        database_url = database_url.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=20,
            command_timeout=30,
            init=lambda conn: conn.set_type_codec(
                'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
            )
        )
    return _pool

async def init_pool():
    """Warm up the asyncpg connection pool. No DDL — use Alembic for schema."""
    await get_pool()


async def init_db():
    """Deprecated alias for init_pool(). DDL now lives in Alembic migrations."""
    await init_pool()


async def _legacy_init_db_ddl():
    """
    LEGACY — kept for reference only. This DDL is now managed by Alembic.
    Do NOT call this function from startup.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS restaurants (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                whatsapp_number TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                menu JSONB NOT NULL DEFAULT '{}'::jsonb,
                subscription_status TEXT NOT NULL DEFAULT 'active',
                features JSONB NOT NULL DEFAULT '{}'::jsonb,
                billing_config JSONB DEFAULT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
            
            CREATE TABLE IF NOT EXISTS billing_log (
                id            SERIAL PRIMARY KEY,
                restaurant_id INTEGER NOT NULL,
                order_id      TEXT    NOT NULL DEFAULT '',
                provider      TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'pending',
                external_id   TEXT    NOT NULL DEFAULT '',
                error_message TEXT    NOT NULL DEFAULT '',
                created_at    TIMESTAMP DEFAULT NOW()
            );
            
            CREATE TABLE IF NOT EXISTS reservations (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                guests INTEGER NOT NULL,
                phone TEXT NOT NULL,
                bot_number TEXT NOT NULL DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW()
            );
            
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                phone TEXT NOT NULL,
                items JSONB NOT NULL,
                order_type TEXT NOT NULL,
                address TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                subtotal INTEGER NOT NULL,
                delivery_fee INTEGER DEFAULT 0,
                total INTEGER NOT NULL,
                status TEXT DEFAULT 'pendiente_pago',
                paid BOOLEAN DEFAULT FALSE,
                payment_url TEXT DEFAULT '',
                transaction_id TEXT DEFAULT '',
                bot_number TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT NOW(),
                paid_at TIMESTAMP
            );
            
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT NOT NULL,
                bot_number TEXT NOT NULL DEFAULT '',
                history JSONB NOT NULL DEFAULT '[]',
                bot_paused BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (phone, bot_number)
            );
            
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                restaurant_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'owner',
                branch_id INTEGER,
                parent_user TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
            
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '72 hours'
            );
            
            CREATE TABLE IF NOT EXISTS carts (
                phone TEXT NOT NULL,
                bot_number TEXT NOT NULL,
                cart_data JSONB NOT NULL DEFAULT '{"items": [], "order_type": null, "address": null, "notes": ""}'::jsonb,
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (phone, bot_number)
            );

            CREATE TABLE IF NOT EXISTS meta_rate_limits (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        
        migrations = [
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS bot_paused BOOLEAN DEFAULT FALSE",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS bot_number TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE reservations ADD COLUMN IF NOT EXISTS bot_number TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS bot_number TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS parent_restaurant_id INTEGER",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS latitude NUMERIC(10,7)",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS longitude NUMERIC(10,7)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id INTEGER",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_user TEXT",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'active'",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS address TEXT DEFAULT ''",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS features JSONB NOT NULL DEFAULT '{}'::jsonb",
            "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '72 hours'",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS billing_config JSONB DEFAULT NULL",
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS google_maps_url TEXT DEFAULT ''",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS wa_phone_id TEXT DEFAULT ''",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS wa_access_token TEXT DEFAULT ''",
        ]
        
        for m in migrations:
            try:
                await conn.execute(m)
            except Exception:
                pass
        try:
            await conn.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_pkey")
            await conn.execute("ALTER TABLE conversations ADD CONSTRAINT conversations_pkey PRIMARY KEY (phone, bot_number)")
        except Exception:
            pass

        # ── schema additions that used to live inside data functions ──
        for col_sql in [
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT DEFAULT ''",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS base_order_id TEXT",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS sub_number INTEGER DEFAULT 1",
        ]:
            try:
                await conn.execute(col_sql)
            except Exception:
                pass

        # ── menu_availability table ───────────────────────────────────
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS menu_availability (
                dish_name TEXT PRIMARY KEY,
                available BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # ── performance indexes ───────────────────────────────────────
        indexes = [
            # orders
            "CREATE INDEX IF NOT EXISTS idx_orders_bot_date     ON orders(bot_number, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_orders_phone        ON orders(phone)",
            "CREATE INDEX IF NOT EXISTS idx_orders_paid_date    ON orders(paid, created_at DESC)",
            # table_orders
            "CREATE INDEX IF NOT EXISTS idx_table_orders_table  ON table_orders(table_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_table_orders_phone  ON table_orders(phone)",
            # waiter_alerts
            "CREATE INDEX IF NOT EXISTS idx_waiter_alerts_bot   ON waiter_alerts(bot_number, dismissed, created_at DESC)",
            # sessions (auth)
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires    ON sessions(expires_at)",
            # reservations
            "CREATE INDEX IF NOT EXISTS idx_reservations_bot    ON reservations(bot_number, date ASC)",
            # conversations
            "CREATE INDEX IF NOT EXISTS idx_convs_updated       ON conversations(bot_number, updated_at DESC)",
            # restaurants
            "CREATE INDEX IF NOT EXISTS idx_restaurants_wa      ON restaurants(whatsapp_number)",
            # rate limiting
            "CREATE INDEX IF NOT EXISTS idx_rate_phone          ON meta_rate_limits(phone)",
        ]
        for idx_sql in indexes:
            try:
                await conn.execute(idx_sql)
            except Exception:
                pass

    print("Database initialized", flush=True)

async def db_init_nps_inventory():
    """Inicializa las tablas de NPS e Inventario — llamar desde main.py en el startup"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS nps_responses (
                id          SERIAL PRIMARY KEY,
                phone       TEXT NOT NULL,
                bot_number  TEXT NOT NULL DEFAULT '',
                score       INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
                comment     TEXT DEFAULT '',
                created_at  TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id              SERIAL PRIMARY KEY,
                restaurant_id   INTEGER NOT NULL,
                name            TEXT NOT NULL,
                unit            TEXT NOT NULL DEFAULT 'unidades',
                current_stock   NUMERIC(10,2) NOT NULL DEFAULT 0,
                min_stock       NUMERIC(10,2) NOT NULL DEFAULT 0,
                linked_dishes   JSONB NOT NULL DEFAULT '[]'::jsonb,
                cost_per_unit   NUMERIC(10,2) DEFAULT 0,
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS inventory_history (
                id              SERIAL PRIMARY KEY,
                inventory_id    INTEGER NOT NULL,
                quantity_delta  NUMERIC(10,2) NOT NULL,
                stock_after     NUMERIC(10,2) NOT NULL,
                reason          TEXT NOT NULL DEFAULT 'ajuste_manual',
                created_at      TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS nps_waiting (
                phone       TEXT NOT NULL,
                bot_number  TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (phone, bot_number)
            );
        """)

        # Índices (ignoramos si ya existen)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_nps_bot_number ON nps_responses(bot_number, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_inventory_restaurant ON inventory(restaurant_id)",
            "CREATE INDEX IF NOT EXISTS idx_inv_history ON inventory_history(inventory_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_nps_waiting_phone ON nps_waiting(phone, bot_number)",
        ]:
            try:
                await conn.execute(idx_sql)
            except Exception:
                pass

        # Migraciones de columnas nuevas en tablas existentes
        for col_sql in [
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS google_maps_url TEXT DEFAULT ''",
        ]:
            try:
                await conn.execute(col_sql)
            except Exception:
                pass

    print("✅ Tablas NPS e Inventario listas", flush=True)

# ── RESERVACIONES ────────────────────────────────────────────────────
async def db_add_reservation(name, date_str, time_str, guests, phone, bot_number: str = "", notes=""):
    """Insert or update reservation. Updates an existing one for same phone/bot/date/time to prevent duplicates."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Use upsert pattern: delete any earlier reservation for same slot, then insert fresh
        existing_id = await conn.fetchval(
            'SELECT id FROM reservations WHERE phone=$1 AND bot_number=$2 AND "date"=$3 AND "time"=$4',
            phone, bot_number, date_str, time_str
        )
        if existing_id:
            row = await conn.fetchrow(
                'UPDATE reservations SET name=$1, guests=$2, notes=$3 WHERE id=$4 RETURNING *',
                name, int(guests), notes, existing_id
            )
        else:
            row = await conn.fetchrow(
                'INSERT INTO reservations (name, "date", "time", guests, phone, bot_number, notes) VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *',
                name, date_str, time_str, int(guests), phone, bot_number, notes
            )
        return _serialize(dict(row))

async def db_get_reservations_range(date_from: str, date_to: str, bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM reservations WHERE date >= $1 AND date <= $2 AND bot_number=$3 ORDER BY date, time", date_from, date_to, bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM reservations WHERE date >= $1 AND date <= $2 ORDER BY date, time", date_from, date_to)
        return [_serialize(dict(r)) for r in rows]

async def db_get_all_reservations(bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM reservations WHERE bot_number=$1 ORDER BY created_at DESC", bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM reservations ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]


# ── ORDENES DELIVERY ─────────────────────────────────────────────────
async def db_save_order(order: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO orders (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, payment_url, bot_number,
                payment_method, base_order_id, sub_number)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items,
                subtotal=EXCLUDED.subtotal,
                total=EXCLUDED.total,
                status=CASE
                    WHEN orders.status IN ('en_preparacion','listo','en_camino','en_puerta','entregado')
                    THEN orders.status
                    ELSE EXCLUDED.status
                END,
                paid=EXCLUDED.paid,
                payment_url=EXCLUDED.payment_url,
                notes=EXCLUDED.notes,
                payment_method=EXCLUDED.payment_method
        """,
        order["id"], order["phone"], json.dumps(order["items"]),
        order["order_type"], order.get("address", ""), order.get("notes", ""),
        order["subtotal"], order["delivery_fee"], order["total"],
        order["status"], order["paid"], order.get("payment_url", ""),
        order.get("bot_number", ""), order.get("payment_method", ""),
        order.get("base_order_id"), order.get("sub_number", 1))

async def db_confirm_payment(order_id: str, transaction_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE orders SET paid=TRUE, status='confirmado', transaction_id=$2, paid_at=NOW()
            WHERE id=$1 RETURNING *
        """, order_id, transaction_id)
        return _serialize(dict(row)) if row else None

async def db_get_orders_range(date_from: str, date_to: str, bot_number: str = None):
    pool = await get_pool()
    d_from = _to_date(date_from)
    d_to_inclusive = _to_date(date_to) + timedelta(days=1)
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2 AND bot_number=$3 ORDER BY created_at DESC", d_from, d_to_inclusive, bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2 ORDER BY created_at DESC", d_from, d_to_inclusive)
        return [_serialize(dict(r)) for r in rows]

async def db_get_order(order_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        return _serialize(dict(row)) if row else None

async def db_get_all_orders(bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM orders WHERE bot_number=$1 ORDER BY created_at DESC", bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]

async def db_get_delivery_orders(status_list: list):
    """Obtiene los pedidos de domicilio filtrados por una lista de estados"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM orders WHERE order_type IN ('domicilio', 'recoger') AND status = ANY($1) ORDER BY created_at ASC",
            status_list
        )
        return [_serialize(dict(r)) for r in rows]

async def db_update_order_status(order_id: str, new_status: str):
    """Actualiza el estado de un pedido y todas sus sub-órdenes con el mismo base_order_id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status=$2 WHERE id=$1", order_id, new_status)
        # Cascade to sub-orders (base order id matches both base_order_id column and the passed id)
        await conn.execute(
            "UPDATE orders SET status=$2 WHERE base_order_id=$1 AND id != $1",
            order_id, new_status
        )

# ── CONVERSACIONES ───────────────────────────────────────────────────
async def db_get_history(phone: str, bot_number: str = "") -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT history FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
        if row:
            h = row["history"]
            return h if isinstance(h, list) else json.loads(h)
        return []

async def db_save_history(phone: str, bot_number: str, history: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, history, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET history=EXCLUDED.history, updated_at=NOW()
        """, phone, bot_number, json.dumps(history[-20:]))

async def db_get_all_conversations(bot_number: str = None, date_from: str = None, date_to: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1

        if bot_number:
            conditions.append(f"bot_number = ${idx}")
            params.append(bot_number)
            idx += 1

        if date_from:
            conditions.append(f"created_at >= ${idx}")
            params.append(_to_date(date_from))
            idx += 1

        if date_to:
            conditions.append(f"created_at < ${idx}")
            params.append(_to_date(date_to) + timedelta(days=1))
            idx += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT phone, bot_number, history, updated_at, created_at FROM conversations {where} ORDER BY updated_at DESC"

        rows = await conn.fetch(query, *params)

        result = []
        for r in rows:
            history = r["history"] if isinstance(r["history"], list) else json.loads(r["history"])
            last_user = next(
                (m["content"] for m in reversed(history)
                 if m["role"] == "user" and isinstance(m.get("content"), str)),
                ""
            )
            result.append({
                "phone": r["phone"],
                "messages": len(history),
                "preview": last_user[:60] if last_user else "...",
                "updated_at": r["updated_at"].isoformat()[:19]
            })
        return result
        
async def db_delete_conversation(phone: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)

async def db_get_conversation_details(phone: str, bot_number: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT history, bot_paused FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
        if row:
            history = row["history"] if isinstance(row["history"], list) else json.loads(row["history"])
            return {"history": history, "bot_paused": row["bot_paused"] or False}
    return {"history": [], "bot_paused": False}

async def db_toggle_bot(phone: str, bot_number: str, pause: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, bot_paused, updated_at)
            VALUES ($1,$2,$3,NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET bot_paused=EXCLUDED.bot_paused, updated_at=NOW()
        """, phone, bot_number, pause)

async def db_cleanup_old_conversations(days: int = 7, bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL AND bot_number=$2", str(days), bot_number)
        else:
            await conn.execute("DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL", str(days))


# ── USUARIOS ─────────────────────────────────────────────────────────
async def db_get_user(username: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE username=$1", username.lower().strip())
        return dict(row) if row else None

async def db_create_user(username: str, password_hash: str, restaurant_name: str,
                          role: str = "owner", branch_id: int = None, parent_user: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id, parent_user)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, username.lower().strip(), password_hash, restaurant_name, role, branch_id, parent_user)
            return True
        except asyncpg.UniqueViolationError:
            return False

async def db_get_all_users():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, restaurant_name, role, branch_id, parent_user FROM users")
        return [dict(r) for r in rows]


# ── RESTAURANTES ─────────────────────────────────────────────────────
async def db_get_restaurant_by_phone(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE whatsapp_number=$1", _normalize_phone(whatsapp_number.strip()))
        return _serialize(dict(row)) if row else None

async def db_get_restaurant_by_bot_number(whatsapp_number: str):
    return await db_get_restaurant_by_phone(whatsapp_number)

async def db_get_restaurant_by_name(name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE name=$1", name)
        return _serialize(dict(row)) if row else None

async def db_get_restaurant_by_id(restaurant_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE id=$1", restaurant_id)
        return _serialize(dict(row)) if row else None

async def db_get_all_restaurants():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM restaurants ORDER BY id ASC")
        return [_serialize(dict(r)) for r in rows]


async def db_check_module(bot_number: str, module_name: str) -> bool:
    """
    Return True if module_name is explicitly enabled (true) in the restaurant's
    features JSONB column.

    Returns False for:
      - Restaurant not found for bot_number
      - Key not present in features
      - Key present but value is not the boolean true (e.g. false, null, string)

    Query is fully parametrized ($1, $2) — no f-strings, no injection risk.

    Example features structure:
        {"staff_tips": true, "reservations": true, "delivery": false}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # ->> extracts the key as TEXT; comparing to 'true' safely handles any
        # non-boolean value stored in the JSONB without risking a cast error.
        # COALESCE turns NULL (restaurant not found, or key absent) into false.
        val = await conn.fetchval(
            "SELECT COALESCE((features->>$2) = 'true', false) "
            "FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(bot_number),
            module_name,
        )
    # fetchval returns None when no rows match; bool(None) == False
    return bool(val)


async def db_create_restaurant(name: str, whatsapp_number: str, address: str, menu: dict,
                                latitude: float = None, longitude: float = None, features: dict = None):
    if features is None: features = {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurants (name, whatsapp_number, address, menu, latitude, longitude, features)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (whatsapp_number) DO UPDATE
            SET name=EXCLUDED.name, address=EXCLUDED.address, menu=EXCLUDED.menu,
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, features=EXCLUDED.features
        """, name, whatsapp_number, address, json.dumps(menu), latitude, longitude, json.dumps(features))

# ── OFFLINE SYNC BATCH ───────────────────────────────────────────────
# Dispatch table for POST /api/sync operations.
# Keys are the `type` field sent by offline-sync.js.
# Each handler receives (conn, restaurant_id, op_data) and performs an upsert.
# New modules (Phase 6+) register handlers here.
_SYNC_HANDLERS: dict = {}


def _register_sync_handler(type_name: str):
    """Decorator to register a sync handler function."""
    def decorator(fn):
        _SYNC_HANDLERS[type_name] = fn
        return fn
    return decorator


@_register_sync_handler("staff_shift")
async def _sync_staff_shift(conn, restaurant_id: int, data: dict):
    """Upsert a staff_shifts record by its client-generated UUID."""
    await conn.execute(
        """
        INSERT INTO staff_shifts
            (id, staff_id, restaurant_id, clock_in, clock_out, notes)
        VALUES ($1, $2::uuid, $3, $4::timestamptz, $5::timestamptz, $6)
        ON CONFLICT (id) DO UPDATE
            SET clock_out = EXCLUDED.clock_out,
                notes     = EXCLUDED.notes
        """,
        data.get("id"),
        data.get("staff_id"),
        restaurant_id,
        data.get("clock_in"),
        data.get("clock_out"),
        data.get("notes", ""),
    )


@_register_sync_handler("staff")
async def _sync_staff(conn, restaurant_id: int, data: dict):
    """Upsert a staff record by its client-generated UUID."""
    await conn.execute(
        """
        INSERT INTO staff
            (id, restaurant_id, name, role, pin, active)
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        ON CONFLICT (id) DO UPDATE
            SET name   = EXCLUDED.name,
                role   = EXCLUDED.role,
                pin    = EXCLUDED.pin,
                active = EXCLUDED.active
        """,
        data.get("id"),
        restaurant_id,
        data.get("name", ""),
        data.get("role", "staff"),
        data.get("pin", ""),
        data.get("active", True),
    )


async def db_sync_batch(restaurant_id: int, operations: list) -> list:
    """
    Process a batch of offline operations.
    Each operation: {id, type, action, data, client_ts}.
    Returns [{id, status: 'ok'|'error'|'unsupported_type', error?}].
    All operations use fully parametrized upserts — no f-string SQL.
    """
    pool = await get_pool()
    results = []
    async with pool.acquire() as conn:
        for op in operations:
            op_id   = op.get("id", "unknown")
            op_type = op.get("type", "")
            handler = _SYNC_HANDLERS.get(op_type)
            if handler is None:
                results.append({
                    "id":     op_id,
                    "status": "unsupported_type",
                    "error":  f"No sync handler registered for type '{op_type}'",
                })
                continue
            try:
                async with conn.transaction():
                    await handler(conn, restaurant_id, op.get("data", {}))
                results.append({"id": op_id, "status": "ok"})
            except Exception as exc:
                results.append({"id": op_id, "status": "error", "error": str(exc)})
    return results


async def db_get_menu(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT menu FROM restaurants WHERE whatsapp_number=$1", whatsapp_number)
        if row and row['menu']:
            return row['menu'] if isinstance(row['menu'], dict) else json.loads(row['menu'])
        return None

async def db_get_top_dishes(whatsapp_number: str, top_n: int = 5):
    menu = await db_get_menu(whatsapp_number)
    if not menu:
        return []
    all_dishes = []
    if isinstance(menu, dict):
        for cat, dishes in menu.items():
            if isinstance(dishes, list):
                all_dishes.extend(dishes)
    return all_dishes[:top_n]

async def db_update_subscription(restaurant_id: int, new_status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE restaurants SET subscription_status=$2 WHERE id=$1", restaurant_id, new_status)


# ── MENU AVAILABILITY ────────────────────────────────────────────────
async def db_get_menu_availability():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT dish_name, available FROM menu_availability")
        return {r['dish_name']: r['available'] for r in rows}

async def db_set_dish_availability(dish_name: str, available: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO menu_availability (dish_name, available, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (dish_name) DO UPDATE SET available=EXCLUDED.available, updated_at=NOW()
        """, dish_name, available)


# ── SESIONES AUTH ────────────────────────────────────────────────────
async def db_save_session(token: str, username: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
        await conn.execute(
            "INSERT INTO sessions (token, username, expires_at) VALUES ($1, $2, $3)",
            token, username, expires
        )

async def db_get_session(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT username FROM sessions WHERE token=$1 AND expires_at > NOW()",
            token
        )
        return row["username"] if row else None

async def db_delete_session(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token=$1", token)

async def db_cleanup_expired_sessions():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            print(f"🧹 Sesiones expiradas eliminadas: {count}", flush=True)


# ── CARRITOS ─────────────────────────────────────────────────────────
async def db_get_cart(phone: str, bot_number: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT cart_data FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number)
        if row:
            return json.loads(row["cart_data"]) if isinstance(row["cart_data"], str) else row["cart_data"]
        return {"items": [], "order_type": None, "address": None, "notes": ""}

async def db_save_cart(phone: str, bot_number: str, cart_data: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO carts (phone, bot_number, cart_data, updated_at)
            VALUES ($1, $2, $3::jsonb, NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data=EXCLUDED.cart_data, updated_at=NOW()
        """, phone, bot_number, json.dumps(cart_data))

async def db_clear_cart(phone: str, bot_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM carts WHERE phone=$1 AND bot_number=$2", phone, bot_number)


# ── MESAS ────────────────────────────────────────────────────────────
async def db_init_tables():
    pool = await get_pool()
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

async def db_get_tables(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if branch_id is not None:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number", branch_id)
        else:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")
        return [_serialize(dict(r)) for r in rows]

async def db_create_table(table_id: str, number: int, name: str, branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, active)
            VALUES ($1,$2,$3,$4,TRUE)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name, branch_id=EXCLUDED.branch_id, active=TRUE
        """, table_id, number, name, branch_id)

async def db_delete_table(table_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE restaurant_tables SET active=FALSE WHERE id=$1", table_id)

async def db_get_table_by_id(table_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurant_tables WHERE id=$1", table_id)
        return _serialize(dict(row)) if row else None

async def db_save_table_order(order: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO table_orders
                (id, table_id, table_name, phone, items, status, notes, total,
                 base_order_id, sub_number, station)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items, status=EXCLUDED.status,
                notes=EXCLUDED.notes, total=EXCLUDED.total, updated_at=NOW()
        """, order['id'], order['table_id'], order['table_name'], order['phone'],
            json.dumps(order['items']),
            order.get('status', 'recibido'), order.get('notes', ''), order.get('total', 0),
            order.get('base_order_id'),
            order.get('sub_number', 1),
            order.get('station', 'all'))

async def db_get_base_order_status(base_order_id: str) -> str | None:
    """Returns the status of the base order record itself (not sub-orders)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM table_orders WHERE id=$1", base_order_id
        )
        return row["status"] if row else None

async def db_merge_table_order_items(base_order_id: str, new_items: list, additional_total: float) -> bool:
    """Merges new items into the base order when it's still in 'recibido' status.
    Combines quantities for duplicate item names. Returns False if order is no longer recibido."""
    pool = await get_pool()
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
        new_total = float(row["total"]) + float(additional_total)
        await conn.execute(
            "UPDATE table_orders SET items=$2, total=$3, updated_at=NOW() WHERE id=$1",
            base_order_id, json.dumps(merged), new_total
        )
        return True

async def db_get_table_orders(status: str = None):
    pool = await get_pool()
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1", order_id, status)

async def db_get_base_order_id(table_id: str) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(base_order_id, id) as base_id
            FROM table_orders
            WHERE table_id=$1 AND status NOT IN ('factura_entregada', 'cancelado')
            ORDER BY created_at ASC LIMIT 1
        """, table_id)
        return row['base_id'] if row else None

async def db_get_next_sub_number(base_order_id: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT MAX(sub_number) as max_sub FROM table_orders WHERE base_order_id=$1 OR id=$1", base_order_id)
        return (row['max_sub'] or 0) + 1

async def db_get_table_bill(base_order_id: str) -> dict:
    pool = await get_pool()
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE table_orders SET status='factura_entregada', updated_at=NOW() WHERE (base_order_id=$1 OR id=$1) AND status NOT IN ('cancelado')", base_order_id)
        return result != "UPDATE 0"

async def db_has_pending_invoice(phone: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM table_orders WHERE phone=$1 AND status='entregado' LIMIT 1", phone)
        return row is not None

async def db_get_active_table_order(phone: str, table_id: str) -> dict | None:
    pool = await get_pool()
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


# ── WAITER ALERTS ────────────────────────────────────────────────────
async def db_init_waiter_alerts():
    pool = await get_pool()
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO waiter_alerts (table_id, table_name, phone, bot_number, alert_type, message) VALUES ($1, $2, $3, $4, $5, $6) RETURNING *", table_id, table_name, phone, bot_number, alert_type, message)
        return _serialize(dict(row))

async def db_get_waiter_alerts(bot_number: str) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM waiter_alerts WHERE bot_number=$1 AND dismissed=FALSE AND created_at > NOW() - INTERVAL '2 hours' ORDER BY created_at DESC", bot_number)
        return [_serialize(dict(r)) for r in rows]

async def db_dismiss_waiter_alert(alert_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("UPDATE waiter_alerts SET dismissed=TRUE WHERE id=$1", alert_id)
        return result == "UPDATE 1"


# ── TABLE SESSIONS ───────────────────────────────────────────────────
async def db_init_table_sessions():
    pool = await get_pool()
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE phone=$1 AND bot_number=$2 AND status='active' ORDER BY started_at DESC LIMIT 1", phone, bot_number)
        return _serialize(dict(row)) if row else None

async def db_create_table_session(phone: str, bot_number: str, table_id: str, table_name: str) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("INSERT INTO table_sessions (phone, bot_number, table_id, table_name, status, last_activity) VALUES ($1, $2, $3, $4, 'active', NOW()) RETURNING *", phone, bot_number, table_id, table_name)
        return _serialize(dict(row))

async def db_touch_session(phone: str, bot_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)

async def db_touch_session_with_phone_id(phone: str, bot_number: str, meta_phone_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET last_activity=NOW(), meta_phone_id=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, meta_phone_id)

async def db_session_mark_order(phone: str, bot_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET has_order=TRUE, last_activity=NOW() WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)

async def db_session_mark_delivered(phone: str, bot_number: str, total: int = 0):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET order_delivered=TRUE, last_activity=NOW(), total_spent=$3 WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number, total)

async def db_close_session(phone: str, bot_number: str, reason: str = "manual", closed_by_username: str = "") -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE table_sessions
            SET status='closed', closed_at=NOW(), closed_by=$3, closed_by_username=$4,
                summary=jsonb_build_object('close_reason',$3::text,'closed_by_user',$4::text)
            WHERE phone=$1 AND bot_number=$2 AND status='active' RETURNING *
        """, phone, bot_number, reason, closed_by_username)
        return _serialize(dict(row)) if row else None

async def db_mark_session_warned(session_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE table_sessions SET inactivity_warned=TRUE WHERE id=$1", session_id)

async def db_get_stale_sessions() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM table_sessions WHERE status='active' AND inactivity_warned=FALSE
            AND ((has_order=FALSE AND last_activity < NOW() - INTERVAL '10 minutes')
              OR (order_delivered=TRUE AND last_activity < NOW() - INTERVAL '60 minutes'))
        """)
        return [_serialize(dict(r)) for r in rows]

async def db_get_closeable_sessions() -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM table_sessions WHERE status='active' AND inactivity_warned=TRUE AND last_activity < NOW() - INTERVAL '5 minutes'")
        return [_serialize(dict(r)) for r in rows]

async def db_get_closed_sessions(bot_number: str, hours: int = 24) -> list:
    hours = max(1, min(hours, 720))
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM table_sessions WHERE bot_number=$1 AND status='closed'"
            " AND closed_at > NOW() - ($2 * INTERVAL '1 hour') ORDER BY closed_at DESC",
            bot_number, hours,
        )
        return [_serialize(dict(r)) for r in rows]

async def db_get_session_by_id(session_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1", session_id)
        return _serialize(dict(row)) if row else None

async def db_reopen_session(session_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT * FROM table_sessions WHERE id=$1 AND status='closed'", session_id)
        if not target: return None
        phone = target["phone"]
        bot_number = target["bot_number"]
        await conn.execute("UPDATE table_sessions SET status='closed', closed_at=NOW(), closed_by='superseded', closed_by_username='' WHERE phone=$1 AND bot_number=$2 AND status='active'", phone, bot_number)
        row = await conn.fetchrow("UPDATE table_sessions SET status='active', closed_at=NULL, closed_by='', closed_by_username='', inactivity_warned=FALSE, last_activity=NOW(), summary=jsonb_build_object('reopened',true) WHERE id=$1 RETURNING *", session_id)
        return _serialize(dict(row)) if row else None

async def db_get_restaurant_settings() -> dict:
    all_r = await db_get_all_restaurants()
    return all_r[0] if all_r else {}
 
async def db_save_nps_response(phone: str, bot_number: str, score: int, comment: str = "") -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO nps_responses (phone, bot_number, score, comment)
               VALUES ($1, $2, $3, $4) RETURNING *""",
            phone, bot_number, score, comment
        )
        return _serialize(dict(row))
 
 
async def db_save_nps_pending(phone: str, bot_number: str, score: int) -> int:
    """Save a preliminary NPS record when score is received but comment is still pending.
    Returns the inserted row id so it can be updated later."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO nps_responses (phone, bot_number, score, comment)
               VALUES ($1, $2, $3, '__pending__') RETURNING id""",
            phone, bot_number, score
        )
        return row["id"] if row else 0


async def db_update_nps_comment(phone: str, bot_number: str, comment: str) -> bool:
    """Update the pending NPS record with the actual comment."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE nps_responses SET comment=$3
               WHERE phone=$1 AND bot_number=$2 AND comment='__pending__'
               AND created_at > NOW() - INTERVAL '24 hours'""",
            phone, bot_number, comment
        )
        return result != "UPDATE 0"


async def db_get_pending_nps_score(phone: str, bot_number: str) -> int | None:
    """Check if there is a pending NPS comment request in the DB (score saved, comment missing)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT score FROM nps_responses
               WHERE phone=$1 AND bot_number=$2 AND comment='__pending__'
               AND created_at > NOW() - INTERVAL '24 hours'
               ORDER BY created_at DESC LIMIT 1""",
            phone, bot_number
        )
        return row["score"] if row else None


async def db_get_nps_stats(bot_number: str, period: str = "month") -> dict:
    pool = await get_pool()
    period_map = {
        "today":    "1 day",
        "week":     "7 days",
        "month":    "30 days",
        "semester": "180 days",
        "year":     "365 days",
    }
    interval = period_map.get(period, "30 days")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT score, COUNT(*) AS count
                FROM nps_responses
                WHERE bot_number = $1
                  AND created_at >= NOW() - INTERVAL '{interval}'
                GROUP BY score
                ORDER BY score""",
            bot_number
        )

    total      = sum(r["count"] for r in rows)
    promoters  = sum(r["count"] for r in rows if r["score"] >= 4)
    detractors = sum(r["count"] for r in rows if r["score"] <= 3)
    score_sum  = 0
    dist       = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    for r in rows:
        dist[r["score"]] = dist.get(r["score"], 0) + r["count"]
        score_sum += r["score"] * r["count"]

    nps_score = round(((promoters - detractors) / total * 100)) if total > 0 else 0
    avg_score = round(score_sum / total, 2) if total > 0 else 0

    return {
        "total":        total,
        "promoters":    promoters,
        "detractors":   detractors,
        "nps_score":    nps_score,
        "avg_score":    avg_score,
        "distribution": dist,
    }

async def db_get_nps_responses(bot_number: str, period: str = "month", limit: int = 50) -> list:
    pool = await get_pool()
    period_map = {
        "today": "1 day", "week": "7 days",
        "month": "30 days", "semester": "180 days", "year": "365 days",
    }
    interval = period_map.get(period, "30 days")
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT * FROM nps_responses
                WHERE bot_number = $1
                  AND created_at >= NOW() - INTERVAL '{interval}'
                  AND comment != '__pending__'
                ORDER BY created_at DESC
                LIMIT $2""",
            bot_number, limit
        )
    return [_serialize(dict(r)) for r in rows]


# ── NPS WAITING STATE (persiste el estado "waiting_score" en DB) ──────

async def db_save_nps_waiting(phone: str, bot_number: str):
    """Persists that we are waiting for an NPS score from this customer.
    Called when trigger_nps is invoked so state survives server restarts."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nps_waiting (phone, bot_number, created_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (phone, bot_number) DO UPDATE SET created_at = NOW()
        """, phone, bot_number)


async def db_get_nps_waiting(phone: str, bot_number: str) -> bool:
    """Returns True if there is a pending NPS score request for this customer (within 48 hours)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM nps_waiting WHERE phone=$1 AND bot_number=$2 AND created_at > NOW() - INTERVAL '48 hours'",
            phone, bot_number
        )
        return row is not None


async def db_clear_nps_waiting(phone: str, bot_number: str):
    """Removes the pending NPS state — called after score is received or survey is skipped."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM nps_waiting WHERE phone=$1 AND bot_number=$2",
            phone, bot_number
        )
        # Prune expired records while we're at it
        await conn.execute(
            "DELETE FROM nps_waiting WHERE created_at < NOW() - INTERVAL '48 hours'"
        )


# ── INVENTARIO ───────────────────────────────────────────────────────
 
async def db_get_inventory(restaurant_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM inventory WHERE restaurant_id = $1 ORDER BY name ASC",
            restaurant_id
        )
    return [_serialize(dict(r)) for r in rows]
 
 
async def db_create_inventory_item(restaurant_id: int, name: str, unit: str,
                                    current_stock: float, min_stock: float,
                                    linked_dishes: list, cost_per_unit: float = 0) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO inventory
               (restaurant_id, name, unit, current_stock, min_stock, linked_dishes, cost_per_unit)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
               RETURNING *""",
            restaurant_id, name, unit, current_stock, min_stock,
            json.dumps(linked_dishes), cost_per_unit
        )
        item = _serialize(dict(row))
        # Si el stock es 0, desactivar platos vinculados
        if current_stock <= 0:
            await _sync_dish_availability(linked_dishes, False)
        return item
 
 
async def db_update_inventory_item(item_id: int, fields: dict) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM inventory WHERE id = $1", item_id)
        if not existing:
            return None
 
        # Construimos el SET dinámico
        allowed = {"name", "unit", "current_stock", "min_stock", "linked_dishes", "cost_per_unit"}
        set_parts = []
        values = []
        idx = 1
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "linked_dishes":
                set_parts.append(f"{k} = ${idx}::jsonb")
                values.append(json.dumps(v))
            else:
                set_parts.append(f"{k} = ${idx}")
                values.append(v)
            idx += 1
 
        if not set_parts:
            return _serialize(dict(existing))
 
        set_parts.append(f"updated_at = NOW()")
        values.append(item_id)
        query = f"UPDATE inventory SET {', '.join(set_parts)} WHERE id = ${idx} RETURNING *"
        row = await conn.fetchrow(query, *values)
        item = _serialize(dict(row))
 
        # Sincronizar disponibilidad si cambió el stock
        new_stock = fields.get("current_stock", existing["current_stock"])
        dishes    = fields.get("linked_dishes", existing["linked_dishes"])
        if isinstance(dishes, str):
            dishes = json.loads(dishes)
        await _sync_dish_availability(dishes, new_stock > 0)
        return item
 
 
async def db_delete_inventory_item(item_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inventory WHERE id = $1", item_id)
 
 
async def db_adjust_inventory_stock(item_id: int, quantity_delta: float,
                                     reason: str, restaurant_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE inventory
               SET current_stock = GREATEST(0, current_stock + $1),
                   updated_at = NOW()
               WHERE id = $2 AND restaurant_id = $3
               RETURNING *""",
            quantity_delta, item_id, restaurant_id
        )
        if not row:
            return None
        item = _serialize(dict(row))
 
        # Registrar en historial
        await conn.execute(
            """INSERT INTO inventory_history (inventory_id, quantity_delta, stock_after, reason)
               VALUES ($1, $2, $3, $4)""",
            item_id, quantity_delta, item["current_stock"], reason
        )
 
        # Sincronizar disponibilidad de platos vinculados
        dishes = item.get("linked_dishes", [])
        if isinstance(dishes, str):
            dishes = json.loads(dishes)
        await _sync_dish_availability(dishes, float(item["current_stock"]) > 0)
        return item
 
 
async def db_get_inventory_history(item_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory_history
               WHERE inventory_id = $1
               ORDER BY created_at DESC
               LIMIT 100""",
            item_id
        )
    return [_serialize(dict(r)) for r in rows]
 
 
async def db_get_inventory_alerts(restaurant_id: int) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT * FROM inventory
               WHERE restaurant_id = $1
                 AND current_stock <= min_stock
               ORDER BY current_stock ASC""",
            restaurant_id
        )
    return [_serialize(dict(r)) for r in rows]
 
 
async def db_deduct_inventory_for_order(bot_number: str, items: list):
    """
    Descuenta stock por cada plato pedido, con soporte para escandallos (dish_recipes).
    items = [{"name": "Hamburguesa Clásica", "quantity": 2}, ...]
    Usa SELECT FOR UPDATE dentro de una transacción para evitar race conditions
    con los 4 workers de Railway.
    Si no hay receta definida, cae al comportamiento legacy de linked_dishes.
    """
    pool = await get_pool()
    restaurant = await db_get_restaurant_by_phone(bot_number)
    if not restaurant:
        return

    restaurant_id = restaurant["id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in items:
                dish_name = item.get("name", "")
                qty       = float(item.get("quantity", item.get("qty", 1)))
                if not dish_name or qty <= 0:
                    continue

                # ── 1. Intentar receta (escandallo) ──────────────────────
                recipe_rows = await conn.fetch(
                    """SELECT r.ingredient_id, r.quantity AS recipe_qty
                       FROM dish_recipes r
                       WHERE r.restaurant_id = $1 AND r.dish_name = $2""",
                    restaurant_id, dish_name
                )

                if recipe_rows:
                    # Bloquear las filas de ingredientes antes de modificar
                    ingredient_ids = [r["ingredient_id"] for r in recipe_rows]
                    locked = await conn.fetch(
                        """SELECT id, current_stock, min_stock, linked_dishes
                           FROM inventory
                           WHERE id = ANY($1::int[])
                           FOR UPDATE""",
                        ingredient_ids
                    )
                    locked_map = {r["id"]: r for r in locked}

                    for rline in recipe_rows:
                        ing_id    = rline["ingredient_id"]
                        deduct    = float(rline["recipe_qty"]) * qty
                        inv       = locked_map.get(ing_id)
                        if not inv:
                            continue
                        new_stock = max(0.0, float(inv["current_stock"]) - deduct)
                        await conn.execute(
                            """UPDATE inventory
                               SET current_stock = $1, updated_at = NOW()
                               WHERE id = $2""",
                            new_stock, ing_id
                        )
                        await conn.execute(
                            """INSERT INTO inventory_history
                               (inventory_id, quantity_delta, stock_after, reason)
                               VALUES ($1, $2, $3, 'orden_confirmada')""",
                            ing_id, -deduct, new_stock
                        )
                        # Desactivar platos vinculados cuando el stock se agota
                        if new_stock <= float(inv["min_stock"] or 0):
                            dishes = inv["linked_dishes"]
                            if isinstance(dishes, str):
                                dishes = json.loads(dishes)
                            if dishes:
                                await _sync_dish_availability_conn(conn, dishes, False)

                else:
                    # ── 2. Fallback legacy: linked_dishes ────────────────
                    rows = await conn.fetch(
                        """SELECT id, current_stock, linked_dishes, min_stock
                           FROM inventory
                           WHERE restaurant_id = $1
                             AND linked_dishes @> $2::jsonb
                           FOR UPDATE""",
                        restaurant_id, json.dumps([dish_name])
                    )
                    for row in rows:
                        new_stock = max(0.0, float(row["current_stock"]) - qty)
                        await conn.execute(
                            """UPDATE inventory
                               SET current_stock = $1, updated_at = NOW()
                               WHERE id = $2""",
                            new_stock, row["id"]
                        )
                        await conn.execute(
                            """INSERT INTO inventory_history
                               (inventory_id, quantity_delta, stock_after, reason)
                               VALUES ($1, $2, $3, 'orden_confirmada')""",
                            row["id"], -qty, new_stock
                        )
                        dishes = row["linked_dishes"]
                        if isinstance(dishes, str):
                            dishes = json.loads(dishes)
                        if new_stock <= float(row["min_stock"] or 0) and dishes:
                            await _sync_dish_availability_conn(conn, dishes, False)
 
 
async def _sync_dish_availability_conn(conn, dish_names: list, available: bool):
    """Activa o desactiva platos usando una conexión existente (dentro de transacción)."""
    for name in dish_names:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, available, updated_at)
               VALUES ($1, $2, NOW())
               ON CONFLICT (dish_name)
               DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
            name, available
        )


async def _sync_dish_availability(dish_names: list, available: bool):
    """Activa o desactiva platos en menu_availability según el stock."""
    if not dish_names:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _sync_dish_availability_conn(conn, dish_names, available)


# ══════════════════════════════════════════════════════════════════════
# ESCANDALLOS / RECETAS (FASE 4)
# ══════════════════════════════════════════════════════════════════════

async def db_init_dish_recipes():
    """
    Crea la tabla dish_recipes (escandallos).
    Mapea platos del menú a sus ingredientes con cantidad exacta por porción.
    Llamar desde main.py en el startup, después de db_init_fiscal_tables().
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dish_recipes (
                id              SERIAL PRIMARY KEY,
                restaurant_id   INTEGER       NOT NULL,
                dish_name       TEXT          NOT NULL,
                ingredient_id   INTEGER       NOT NULL REFERENCES inventory(id) ON DELETE CASCADE,
                quantity        NUMERIC(10,4) NOT NULL CHECK (quantity > 0),
                created_at      TIMESTAMP DEFAULT NOW(),
                updated_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE (restaurant_id, dish_name, ingredient_id)
            )
        """)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_dish_recipes_lookup     ON dish_recipes(restaurant_id, dish_name)",
            "CREATE INDEX IF NOT EXISTS idx_dish_recipes_ingredient ON dish_recipes(ingredient_id)",
        ]:
            await conn.execute(idx_sql)
    print("Dish recipes table ready", flush=True)


async def db_upsert_dish_recipe(restaurant_id: int, dish_name: str, lines: list) -> list:
    """
    Reemplaza el escandallo completo de un plato.
    lines = [{"ingredient_id": int, "quantity": float}, ...]
    Pasar lines=[] para eliminar la receta.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM dish_recipes WHERE restaurant_id=$1 AND dish_name=$2",
                restaurant_id, dish_name
            )
            for line in lines:
                await conn.execute(
                    """INSERT INTO dish_recipes (restaurant_id, dish_name, ingredient_id, quantity)
                       VALUES ($1, $2, $3, $4)""",
                    restaurant_id, dish_name,
                    int(line["ingredient_id"]), float(line["quantity"])
                )
    return await db_get_dish_recipe(restaurant_id, dish_name)


async def db_get_dish_recipe(restaurant_id: int, dish_name: str) -> list:
    """Devuelve las líneas de ingredientes de un plato, con costo por línea."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.id, r.ingredient_id, r.quantity,
                   i.name AS ingredient_name, i.unit, i.cost_per_unit,
                   ROUND((r.quantity * i.cost_per_unit)::numeric, 2) AS line_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1 AND r.dish_name = $2
            ORDER BY i.name
        """, restaurant_id, dish_name)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_recipes(restaurant_id: int) -> list:
    """Lista todos los escandallos con food cost total por plato."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT r.dish_name,
                   COUNT(*) AS ingredient_count,
                   ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]


async def db_delete_dish_recipe(restaurant_id: int, dish_name: str):
    """Elimina todos los ingredientes del escandallo de un plato."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM dish_recipes WHERE restaurant_id=$1 AND dish_name=$2",
            restaurant_id, dish_name
        )


async def db_get_food_costs(restaurant_id: int) -> list:
    """
    Devuelve el Food Cost de cada plato que tiene escandallo definido.
    Incluye desglose por ingrediente para que el dueño vea de dónde viene el costo.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                r.dish_name,
                ROUND(SUM(r.quantity * i.cost_per_unit)::numeric, 2) AS food_cost,
                json_agg(
                    json_build_object(
                        'ingredient',    i.name,
                        'unit',          i.unit,
                        'quantity',      r.quantity,
                        'cost_per_unit', i.cost_per_unit,
                        'line_cost',     ROUND((r.quantity * i.cost_per_unit)::numeric, 2)
                    ) ORDER BY i.name
                ) AS breakdown
            FROM dish_recipes r
            JOIN inventory i ON r.ingredient_id = i.id
            WHERE r.restaurant_id = $1
            GROUP BY r.dish_name
            ORDER BY r.dish_name
        """, restaurant_id)
        return [_serialize(dict(r)) for r in rows]


# ══════════════════════════════════════════════════════════════════════
# FACTURACIÓN ELECTRÓNICA DIAN — TABLAS FISCALES
# ══════════════════════════════════════════════════════════════════════

async def db_init_fiscal_tables():
    """
    Crea las tablas de Facturación Electrónica DIAN.
    fiscal_resolution: resolución DIAN por restaurante (prefijo, rango, ClTec).
    fiscal_invoices:   registro inmutable de cada factura emitida con sus datos fiscales.
    Llamar desde main.py en el startup, después de db_init_nps_inventory().
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fiscal_resolution (
                id                SERIAL PRIMARY KEY,
                restaurant_id     INTEGER NOT NULL UNIQUE,
                resolution_number TEXT    NOT NULL,
                resolution_date   DATE    NOT NULL,
                prefix            TEXT    NOT NULL DEFAULT '',
                from_number       INTEGER NOT NULL,
                to_number         INTEGER NOT NULL,
                valid_from        DATE    NOT NULL,
                valid_to          DATE    NOT NULL,
                technical_key     TEXT    NOT NULL,
                current_number    INTEGER NOT NULL DEFAULT 0,
                environment       TEXT    NOT NULL DEFAULT 'test',
                software_id       TEXT    NOT NULL DEFAULT '',
                software_pin      TEXT    NOT NULL DEFAULT '',
                updated_at        TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS fiscal_invoices (
                id                  SERIAL PRIMARY KEY,
                billing_log_id      INTEGER REFERENCES billing_log(id) ON DELETE SET NULL,
                restaurant_id       INTEGER NOT NULL,
                order_id            TEXT    NOT NULL,

                resolution_number   TEXT    NOT NULL,
                prefix              TEXT    NOT NULL DEFAULT '',
                invoice_number      INTEGER NOT NULL,

                issue_date          DATE      NOT NULL DEFAULT CURRENT_DATE,
                issue_time          TIME      NOT NULL DEFAULT CURRENT_TIME,

                subtotal_cents      BIGINT    NOT NULL DEFAULT 0,
                tax_regime          TEXT      NOT NULL DEFAULT 'iva',
                tax_pct             NUMERIC(5,2) NOT NULL DEFAULT 19.00,
                tax_cents           BIGINT    NOT NULL DEFAULT 0,
                total_cents         BIGINT    NOT NULL DEFAULT 0,

                cufe                TEXT      NOT NULL DEFAULT '',
                qr_data             TEXT      NOT NULL DEFAULT '',
                uuid_dian           TEXT      NOT NULL DEFAULT '',

                xml_content         TEXT,
                pdf_url             TEXT,

                customer_nit        TEXT      NOT NULL DEFAULT '222222222',
                customer_name       TEXT      NOT NULL DEFAULT 'Consumidor Final',
                customer_email      TEXT      NOT NULL DEFAULT '',
                customer_id_type    TEXT      NOT NULL DEFAULT '13',

                payment_method      TEXT      NOT NULL DEFAULT 'cash',

                dian_status         TEXT      NOT NULL DEFAULT 'draft',
                dian_response       JSONB     DEFAULT NULL,

                created_at          TIMESTAMP DEFAULT NOW(),

                UNIQUE (restaurant_id, resolution_number, invoice_number)
            );
        """)

        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_fiscal_invoices_restaurant ON fiscal_invoices(restaurant_id, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_fiscal_invoices_order ON fiscal_invoices(order_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fiscal_invoices_cufe ON fiscal_invoices(cufe) WHERE cufe != ''",
        ]:
            try:
                await conn.execute(idx_sql)
            except Exception:
                pass

    print("Fiscal tables initialized", flush=True)


async def db_get_fiscal_resolution(restaurant_id: int) -> dict | None:
    """Devuelve la resolución DIAN activa del restaurante, o None si no existe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM fiscal_resolution WHERE restaurant_id=$1",
            restaurant_id
        )
    return _serialize(dict(row)) if row else None


async def db_upsert_fiscal_resolution(restaurant_id: int, data: dict) -> None:
    """Inserta o actualiza la resolución DIAN de un restaurante."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO fiscal_resolution
               (restaurant_id, resolution_number, resolution_date, prefix,
                from_number, to_number, valid_from, valid_to,
                technical_key, current_number, environment, software_id, software_pin)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (restaurant_id) DO UPDATE SET
                 resolution_number = EXCLUDED.resolution_number,
                 resolution_date   = EXCLUDED.resolution_date,
                 prefix            = EXCLUDED.prefix,
                 from_number       = EXCLUDED.from_number,
                 to_number         = EXCLUDED.to_number,
                 valid_from        = EXCLUDED.valid_from,
                 valid_to          = EXCLUDED.valid_to,
                 technical_key     = EXCLUDED.technical_key,
                 environment       = EXCLUDED.environment,
                 software_id       = EXCLUDED.software_id,
                 software_pin      = EXCLUDED.software_pin,
                 updated_at        = NOW()""",
            restaurant_id,
            data["resolution_number"], data["resolution_date"], data.get("prefix", ""),
            data["from_number"], data["to_number"],
            data["valid_from"], data["valid_to"],
            data["technical_key"], data.get("current_number", 0),
            data.get("environment", "test"),
            data.get("software_id", ""), data.get("software_pin", ""),
        )


async def db_claim_next_invoice_number(restaurant_id: int) -> int:
    """
    Incrementa atómicamente el consecutivo de factura y lo devuelve.
    Lanza RuntimeError si la resolución no existe o el rango está agotado.
    La operación es atómica (UPDATE ... RETURNING) — segura con múltiples workers.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE fiscal_resolution
               SET current_number = current_number + 1,
                   updated_at     = NOW()
               WHERE restaurant_id = $1
                 AND current_number + 1 <= to_number
               RETURNING current_number, from_number, to_number,
                         valid_from, valid_to, resolution_number, prefix""",
            restaurant_id
        )
    if not row:
        # Puede ser: no existe resolución, rango agotado, o resolución vencida
        res = await db_get_fiscal_resolution(restaurant_id)
        if not res:
            raise RuntimeError("No hay resolución DIAN configurada para este restaurante")
        if res["current_number"] >= res["to_number"]:
            raise RuntimeError(
                f"Rango de facturación agotado ({res['from_number']}-{res['to_number']}). "
                "Solicita una nueva resolución ante la DIAN."
            )
        raise RuntimeError("Error desconocido al reclamar número de factura")
    return row["current_number"]


async def db_save_fiscal_invoice(data: dict) -> int:
    """Persiste la factura electrónica. Devuelve el ID generado."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO fiscal_invoices
               (billing_log_id, restaurant_id, order_id,
                resolution_number, prefix, invoice_number,
                issue_date, issue_time,
                subtotal_cents, tax_regime, tax_pct, tax_cents, total_cents,
                cufe, qr_data, uuid_dian, xml_content, pdf_url,
                customer_nit, customer_name, customer_email, customer_id_type,
                payment_method, dian_status, dian_response)
               VALUES
               ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
                $19,$20,$21,$22,$23,$24,$25)
               RETURNING id""",
            data.get("billing_log_id"),
            data["restaurant_id"], data["order_id"],
            data["resolution_number"], data.get("prefix", ""), data["invoice_number"],
            data.get("issue_date"), data.get("issue_time"),
            data["subtotal_cents"], data.get("tax_regime", "iva"),
            data["tax_pct"], data["tax_cents"], data["total_cents"],
            data.get("cufe", ""), data.get("qr_data", ""), data.get("uuid_dian", ""),
            data.get("xml_content"), data.get("pdf_url"),
            data.get("customer_nit", "222222222"),
            data.get("customer_name", "Consumidor Final"),
            data.get("customer_email", ""),
            data.get("customer_id_type", "13"),
            data.get("payment_method", "cash"),
            data.get("dian_status", "draft"),
            json.dumps(data["dian_response"]) if data.get("dian_response") else None,
        )
    return row["id"]


async def db_get_fiscal_invoices(restaurant_id: int, limit: int = 50) -> list:
    """Lista las facturas electrónicas emitidas por el restaurante."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, order_id, prefix, invoice_number, issue_date,
                      subtotal_cents, tax_regime, tax_pct, tax_cents, total_cents,
                      cufe, qr_data, customer_nit, customer_name,
                      payment_method, dian_status, created_at
               FROM fiscal_invoices
               WHERE restaurant_id=$1
               ORDER BY created_at DESC LIMIT $2""",
            restaurant_id, limit
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_next_invoice_number(
    restaurant_id: int,
    prefix: str,
    start_at: int = 5200,
) -> int:
    """
    Retorna MAX(invoice_number)+1 para el restaurante y prefijo indicados.
    Devuelve start_at si no existen facturas previas con ese prefijo.
    NOTA: SELECT no-atómico — apropiado para sandbox. En producción multi-worker
    usar db_claim_next_invoice_number (UPDATE … RETURNING atómico).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """SELECT COALESCE(MAX(invoice_number), $3 - 1) + 1
               FROM fiscal_invoices
               WHERE restaurant_id = $1 AND prefix = $2""",
            restaurant_id, prefix, start_at,
        )
    return int(val)


async def db_update_invoice_dian_data(
    fiscal_invoice_id: int,
    cufe: str,
    pdf_url: str,
    qr_data: str,
    dian_response: dict | None = None,
) -> None:
    """
    Almacena los 3 campos DIAN retornados por MATIAS API tras la emisión exitosa:
    CUFE, URL/base64 del PDF y cadena QR. Actualiza dian_status a 'accepted'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE fiscal_invoices
               SET cufe          = $2,
                   pdf_url       = $3,
                   qr_data       = $4,
                   dian_status   = 'accepted',
                   dian_response = $5::jsonb
               WHERE id = $1""",
            fiscal_invoice_id,
            cufe,
            pdf_url,
            qr_data,
            json.dumps(dian_response) if dian_response else None,
        )


# ══════════════════════════════════════════════════════════════════════
# SPLIT CHECKS / PAGOS MIXTOS (FASE 5)
# ══════════════════════════════════════════════════════════════════════

async def db_get_order_ticket_data(base_order_id: str, branch_id: int = None) -> dict | None:
    """
    Retorna los ítems y total agregados de todas las sub-órdenes de un ticket.
    Usado por create_checks para validar cantidades antes de crear la división.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if branch_id:
            rows = await conn.fetch(
                """SELECT o.* FROM table_orders o
                   LEFT JOIN restaurant_tables t ON o.table_id = t.id
                   WHERE (o.id = $1 OR o.base_order_id = $1)
                     AND (t.branch_id = $2 OR t.branch_id IS NULL)
                   ORDER BY o.created_at ASC""",
                base_order_id, branch_id
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM table_orders
                   WHERE id=$1 OR base_order_id=$1
                   ORDER BY created_at ASC""",
                base_order_id
            )
    if not rows:
        return None
    all_items = []
    total = 0.0
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
        total += float(d.get("total") or 0)
    return {
        "base_order_id": base_order_id,
        "table_name": first.get("table_name", ""),
        "items": all_items,
        "total": total,
    }

async def db_init_table_checks():
    """
    Crea la tabla table_checks para división de cuentas y pagos mixtos.
    Cada check es una unidad de cobro independiente con su propia factura DIAN.
    Llamar desde main.py en el startup.
    """
    pool = await get_pool()
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
    pool = await get_pool()
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
                    float(c["subtotal"]), float(c["tax_amount"]), float(c["total"])
                )
                inserted_ids.append(check_id)
        rows = await conn.fetch(
            "SELECT * FROM table_checks WHERE base_order_id=$1 ORDER BY check_number",
            base_order_id
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_checks(base_order_id: str) -> list:
    """Devuelve todos los checks de un ticket, con datos fiscales si existen."""
    pool = await get_pool()
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
    pool = await get_pool()
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
) -> None:
    """
    Atómicamente:
    1. Actualiza el check a status='invoiced' con pagos y cambio.
    2. Si TODOS los checks del base_order_id están en {invoiced, cancelled},
       actualiza table_orders a status='factura_entregada'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """UPDATE table_checks
                   SET payments=$1::jsonb, change_amount=$2,
                       fiscal_invoice_id=$3, status='invoiced',
                       customer_name=$4, customer_nit=$5, customer_email=$6,
                       paid_at=NOW()
                   WHERE id=$7""",
                json.dumps(payments), float(change_amount),
                fiscal_invoice_id,
                customer_name, customer_nit, customer_email,
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM table_checks WHERE id=$1 AND status='open'", check_id
        )
    return result != "DELETE 0"


async def db_get_check_ticket(check_id: str) -> dict | None:
    """Devuelve datos del check + info fiscal para impresión de factura."""
    pool = await get_pool()
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


# ══════════════════════════════════════════════════════════════════════
# FASE 6 — STAFF, RELOJ CHECADOR Y PROPINAS
# ══════════════════════════════════════════════════════════════════════

# ── Staff roster ─────────────────────────────────────────────────────

async def db_get_staff(restaurant_id: int) -> list:
    """Return all active (and inactive) staff members for a restaurant."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, restaurant_id, name, role, active, phone, "
            "created_at, updated_at FROM staff "
            "WHERE restaurant_id=$1 ORDER BY name ASC",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_create_staff(
    restaurant_id: int,
    name: str,
    role: str,
    pin_hash: str,
    phone: str = "",
) -> dict:
    """Insert a new staff member. Returns the created row."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff (restaurant_id, name, role, pin, phone)
               VALUES ($1, $2, $3, $4, $5)
               RETURNING id::text, restaurant_id, name, role, active, phone,
                         created_at, updated_at""",
            restaurant_id, name, role, pin_hash, phone,
        )
    return _serialize(dict(row))


async def db_update_staff(staff_id: str, restaurant_id: int, fields: dict) -> dict | None:
    """
    Update mutable staff fields (name, role, pin, phone, active).
    Ignores unknown keys. Returns updated row or None if not found.
    Only updates columns that are explicitly passed in fields.
    All values are passed as parameters — no f-string SQL.
    """
    allowed = {"name", "role", "pin", "phone", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build SET clause with positional params starting at $3
        set_parts = []
        values = []
        for i, (col, val) in enumerate(updates.items(), start=3):
            set_parts.append(f"{col}=${i}")
            values.append(val)

        sql = (
            f"UPDATE staff SET {', '.join(set_parts)}, updated_at=NOW() "
            f"WHERE id=$1::uuid AND restaurant_id=$2 "
            f"RETURNING id::text, restaurant_id, name, role, active, phone, "
            f"created_at, updated_at"
        )
        row = await conn.fetchrow(sql, staff_id, restaurant_id, *values)
    return _serialize(dict(row)) if row else None


# ── Clock-in / Clock-out ─────────────────────────────────────────────

async def db_clock_in(staff_id: str, restaurant_id: int) -> dict:
    """
    Open a new shift for staff_id.
    Raises ValueError if the employee already has an open shift
    (caught via asyncpg.UniqueViolationError from uq_staff_shifts_one_open).
    Returns the new shift dict.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO staff_shifts (staff_id, restaurant_id)
                   VALUES ($1::uuid, $2)
                   RETURNING id::text, staff_id::text, restaurant_id,
                             clock_in, clock_out, notes, created_at""",
                staff_id, restaurant_id,
            )
            return _serialize(dict(row))
        except asyncpg.UniqueViolationError:
            raise ValueError("El empleado ya tiene un turno abierto.")


async def db_clock_out(staff_id: str, restaurant_id: int) -> dict | None:
    """
    Close the open shift for staff_id.
    Returns the updated shift, or None if no open shift was found.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE staff_shifts
               SET clock_out = NOW()
               WHERE staff_id = $1::uuid
                 AND restaurant_id = $2
                 AND clock_out IS NULL
               RETURNING id::text, staff_id::text, restaurant_id,
                         clock_in, clock_out, notes, created_at""",
            staff_id, restaurant_id,
        )
    return _serialize(dict(row)) if row else None


async def db_get_open_shifts(restaurant_id: int) -> list:
    """Return all currently open shifts for a restaurant, joined with staff info."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ss.id::text, ss.staff_id::text, ss.clock_in,
                      s.name AS staff_name, s.role AS staff_role
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id=$1 AND ss.clock_out IS NULL
               ORDER BY ss.clock_in ASC""",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_shifts(
    restaurant_id: int,
    date_from: str,
    date_to: str,
) -> list:
    """Return closed and open shifts in [date_from, date_to] with staff name/role."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT ss.id::text, ss.staff_id::text, ss.clock_in, ss.clock_out,
                      ss.notes,
                      s.name AS staff_name, s.role AS staff_role,
                      EXTRACT(EPOCH FROM (
                          COALESCE(ss.clock_out, NOW()) - ss.clock_in
                      )) / 3600.0 AS hours_worked
               FROM staff_shifts ss
               JOIN staff s ON ss.staff_id = s.id
               WHERE ss.restaurant_id=$1
                 AND ss.clock_in >= $2::timestamptz
                 AND ss.clock_in <  $3::timestamptz
               ORDER BY ss.clock_in DESC""",
            restaurant_id, date_from, date_to,
        )
    return [_serialize(dict(r)) for r in rows]


# ── Tip pool calculation ─────────────────────────────────────────────

async def db_calculate_tip_pool(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    total_tips: float,
) -> dict:
    """
    Calculate tip distribution for [period_start, period_end].

    Algorithm:
      1. Read pct_config from restaurants.features->'tip_distribution'.
         Expected format: {"mesero": 50, "cocina": 30, "bar": 20}
      2. Find all shifts that overlap the period and compute effective hours
         within the window (PostgreSQL handles all timestamp math).
      3. For each configured role: distribute role_pct% of total_tips
         proportionally to hours worked among employees in that role.

    Returns:
      {
        "pct_config": {"mesero": 50, ...},
        "entries":    [{"staff_id", "name", "role", "hours", "amount", "pct"}],
        "total_allocated":   float,
        "total_unallocated": float
      }
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Read tip_distribution config — single parametrized query
        pct_val = await conn.fetchval(
            "SELECT features->'tip_distribution' FROM restaurants WHERE id=$1",
            restaurant_id,
        )
        pct_config: dict = pct_val if isinstance(pct_val, dict) else {}

        if not pct_config:
            return {
                "pct_config": {},
                "entries": [],
                "total_allocated": 0.0,
                "total_unallocated": round(float(total_tips), 2),
            }

        # 2. Compute effective hours per employee within the period.
        # LEAST/GREATEST/COALESCE handle partial overlap and open shifts cleanly.
        rows = await conn.fetch(
            """
            SELECT
                ss.staff_id::text,
                s.name,
                s.role,
                ROUND(
                    CAST(SUM(
                        EXTRACT(EPOCH FROM (
                            LEAST(COALESCE(ss.clock_out, $3::timestamptz), $3::timestamptz)
                            - GREATEST(ss.clock_in, $2::timestamptz)
                        ))
                    ) / 3600.0 AS numeric), 2
                ) AS effective_hours
            FROM staff_shifts ss
            JOIN staff s ON ss.staff_id = s.id
            WHERE ss.restaurant_id = $1
              AND ss.clock_in  < $3::timestamptz
              AND (ss.clock_out > $2::timestamptz OR ss.clock_out IS NULL)
            GROUP BY ss.staff_id, s.name, s.role
            HAVING SUM(
                EXTRACT(EPOCH FROM (
                    LEAST(COALESCE(ss.clock_out, $3::timestamptz), $3::timestamptz)
                    - GREATEST(ss.clock_in, $2::timestamptz)
                ))
            ) > 0
            """,
            restaurant_id, period_start, period_end,
        )

        # 3. Distribute tips by role
        entries = []
        total_allocated = 0.0

        for role, pct in pct_config.items():
            role_emps = [r for r in rows if r["role"] == role]
            if not role_emps:
                continue  # role has no hours in this period — skip

            role_pool = float(total_tips) * (float(pct) / 100.0)
            total_role_hours = sum(float(r["effective_hours"]) for r in role_emps)

            for emp in role_emps:
                h = float(emp["effective_hours"])
                if total_role_hours > 0:
                    amount = role_pool * (h / total_role_hours)
                else:
                    amount = role_pool / len(role_emps)

                entries.append({
                    "staff_id": emp["staff_id"],
                    "name":     emp["name"],
                    "role":     role,
                    "hours":    float(emp["effective_hours"]),
                    "amount":   round(amount, 2),
                    "pct":      pct,
                })
                total_allocated += amount

    return {
        "pct_config":        pct_config,
        "entries":           entries,
        "total_allocated":   round(total_allocated, 2),
        "total_unallocated": round(float(total_tips) - total_allocated, 2),
    }


async def db_save_tip_distribution(
    restaurant_id: int,
    period_start: str,
    period_end: str,
    total_tips: float,
    distribution: list,
    pct_config: dict,
    created_by: str,
) -> dict:
    """Persist a tip distribution cut. Returns the saved row."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO tip_distributions
               (restaurant_id, period_start, period_end,
                total_tips, distribution, pct_config, created_by)
               VALUES ($1, $2::timestamptz, $3::timestamptz,
                       $4, $5::jsonb, $6::jsonb, $7)
               RETURNING id::text, restaurant_id, period_start, period_end,
                         total_tips, distribution, pct_config, created_by, created_at""",
            restaurant_id,
            period_start,
            period_end,
            float(total_tips),
            json.dumps(distribution),
            json.dumps(pct_config),
            created_by,
        )
    return _serialize(dict(row))


async def db_get_tip_distributions(restaurant_id: int, limit: int = 20) -> list:
    """Return recent tip distribution cuts for a restaurant."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id::text, restaurant_id, period_start, period_end,
                      total_tips, distribution, pct_config, created_by, created_at
               FROM tip_distributions
               WHERE restaurant_id=$1
               ORDER BY created_at DESC
               LIMIT $2""",
            restaurant_id, limit,
        )
    return [_serialize(dict(r)) for r in rows]