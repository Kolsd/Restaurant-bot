import os
import asyncpg
import json
from datetime import date, datetime, timedelta


# ══════════════════════════════════════════════════════════════════════
# EXCEPCIONES DE NEGOCIO
# ══════════════════════════════════════════════════════════════════════

class UsageLimitExceeded(Exception):
    """
    Se lanza cuando un restaurante supera su límite diario de tokens o facturas.
    Los límites se configuran en restaurants.features.plan_limits:
      { "daily_tokens": 100000, "daily_invoices": 50 }
    """
    def __init__(self, resource: str, used: int, limit: int):
        self.resource = resource
        self.used     = used
        self.limit    = limit
        super().__init__(
            f"Límite diario de {resource} alcanzado: {used:,}/{limit:,}. "
            "Actualiza tu plan para continuar."
        )

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
            "CREATE INDEX IF NOT EXISTS idx_table_orders_base    ON table_orders(base_order_id)",
            "CREATE INDEX IF NOT EXISTS idx_table_orders_station ON table_orders(station)",
            "CREATE INDEX IF NOT EXISTS idx_rest_tables_lookup   ON restaurant_tables(number, branch_id)", # <-- ÍNDICE NUEVO DE RENDIMIENTO
        ]:
            try: await conn.execute(idx_sql)
            except Exception: pass

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

async def db_save_history(phone: str, bot_number: str, history: list, branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 🛡️ Agregamos branch_id al INSERT y al UPDATE
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, history, branch_id, updated_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (phone, bot_number) 
            DO UPDATE SET history=EXCLUDED.history, branch_id=EXCLUDED.branch_id, updated_at=NOW()
        """, phone, bot_number, json.dumps(history[-20:]), branch_id)

async def db_get_all_conversations(bot_number: str = None, branch_id: int | str = None, date_from: str = None, date_to: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        conditions = []
        params = []
        idx = 1

        if bot_number:
            conditions.append(f"bot_number = ${idx}")
            params.append(bot_number)
            idx += 1

        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass # Sin filtro, trae las conversaciones de toda la franquicia
        elif branch_id is not None:
            conditions.append(f"branch_id = ${idx}")
            params.append(branch_id)
            idx += 1
        elif bot_number:
            conditions.append("branch_id IS NULL")

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

async def db_get_all_restaurants(parent_id: int = None):
    """
    Si se pasa parent_id, solo devuelve las sucursales de ese padre.
    Si no, devuelve todos los restaurantes principales.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if parent_id:
            # Solo devuelve hijos reales
            rows = await conn.fetch(
                "SELECT * FROM restaurants WHERE parent_restaurant_id = $1 ORDER BY name ASC", 
                parent_id
            )
        else:
            # Devuelve solo los que no tienen padre (Matrices)
            rows = await conn.fetch(
                "SELECT * FROM restaurants WHERE parent_restaurant_id IS NULL ORDER BY id ASC"
            )
        return [_serialize(dict(r)) for r in rows]

async def db_find_nearest_branch(customer_lat: float, customer_lon: float, parent_id: int) -> dict | None:
    """Finds the nearest branch to the customer's location within its delivery_radius_km.
    Uses the Haversine formula via PostgreSQL.
    Returns the nearest branch within coverage, or None if no branch covers that area."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name, whatsapp_number, latitude, longitude,
                   features->>'delivery_radius_km' AS radius_km,
                   (
                     6371 * acos(
                       cos(radians($1)) * cos(radians(latitude::float))
                       * cos(radians(longitude::float) - radians($2))
                       + sin(radians($1)) * sin(radians(latitude::float))
                     )
                   ) AS distance_km
            FROM restaurants
            WHERE parent_restaurant_id = $3
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
            ORDER BY distance_km ASC
        """, customer_lat, customer_lon, parent_id)
        for r in rows:
            radius = float(r["radius_km"] or 5.0)
            if r["distance_km"] is not None and r["distance_km"] <= radius:
                return _serialize(dict(r))
    return None

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
        # 🛡️ FIX: Los diccionarios entran directo a asyncpg (sin json.dumps)
        await conn.execute("""
            INSERT INTO restaurants (name, whatsapp_number, address, menu, latitude, longitude, features)
            VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)
            ON CONFLICT (whatsapp_number) DO UPDATE
            SET name=EXCLUDED.name, address=EXCLUDED.address, menu=EXCLUDED.menu,
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude, features=EXCLUDED.features
        """, name, whatsapp_number, address, menu, latitude, longitude, features)

async def db_sync_menu_to_branches(parent_restaurant_id: int) -> int:
    """
    Sincroniza (sobrescribe) la columna 'menu' de todas las sucursales hijas
    con el JSON exacto de la Casa Matriz.
    Devuelve el número de sucursales actualizadas.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Obtener el menú de la matriz
        parent = await conn.fetchrow("SELECT menu FROM restaurants WHERE id = $1", parent_restaurant_id)
        if not parent or not parent["menu"]:
            return 0
            
        menu_jsonb = parent["menu"]
        
        # 2. Hacer UPDATE masivo en las sucursales hijas
        result = await conn.execute(
            "UPDATE restaurants SET menu = $1 WHERE parent_restaurant_id = $2",
            menu_jsonb, parent_restaurant_id
        )
        
        # El result de execute suele ser un string como "UPDATE 3"
        try:
            count = int(result.split()[-1])
        except:
            count = 0
            
        return count

async def db_update_menu(restaurant_id: int, menu_data: dict) -> bool:
    """Sobrescribe el JSON del menú para un restaurante específico."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE restaurants SET menu = $1::jsonb WHERE id = $2",
            menu_data, restaurant_id
        )
        return result == "UPDATE 1"

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
    import json
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 
                r.menu, 
                p.menu AS parent_menu
            FROM restaurants r
            LEFT JOIN restaurants p ON r.parent_restaurant_id = p.id
            WHERE r.whatsapp_number = $1
        """, whatsapp_number)
        
        if not row:
            return None
            
        menu_data = row['menu']
        
        if (not menu_data or menu_data == '{}' or menu_data == "{}") and row['parent_menu']:
            menu_data = row['parent_menu']
            
        # 🛡️ AUTO-SANADOR: Repara cadenas doblemente codificadas al vuelo
        if menu_data:
            if isinstance(menu_data, str):
                try:
                    parsed = json.loads(menu_data)
                    if isinstance(parsed, str):
                        parsed = json.loads(parsed)
                    return parsed
                except:
                    return {}
            return menu_data
        return {}

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

async def db_delete_branch(branch_id: int, owner_restaurant_id: int) -> bool:
    """
    Borra una sucursal SOLO si es hija del restaurante que hace la petición.
    Jamás permite borrar un restaurante que no tenga parent_restaurant_id.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # El WHERE asegura que:
        # 1. El ID coincida.
        # 2. El padre sea el restaurante del usuario actual (seguridad).
        # 3. parent_restaurant_id NO sea NULL (protección contra borrar la matriz).
        result = await conn.execute(
            """DELETE FROM restaurants 
               WHERE id = $1 
               AND parent_restaurant_id = $2 
               AND parent_restaurant_id IS NOT NULL""",
            branch_id, owner_restaurant_id
        )
        return result != "DELETE 0"

# ── MENU AVAILABILITY ────────────────────────────────────────────────
async def db_get_menu_availability(restaurant_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT dish_name, available FROM menu_availability WHERE restaurant_id = $1", restaurant_id)
        return {r['dish_name']: r['available'] for r in rows}

async def db_set_dish_availability(restaurant_id: int, dish_name: str, available: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO menu_availability (dish_name, restaurant_id, available, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (dish_name, restaurant_id) DO UPDATE SET available=EXCLUDED.available, updated_at=NOW()
        """, dish_name, restaurant_id, available)

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

# 🛡️ NUEVO: Migrar el carrito atómicamente a otra sucursal
async def db_migrate_cart(phone: str, from_bot_number: str, to_bot_number: str):
    if from_bot_number == to_bot_number:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT cart_data FROM carts WHERE phone=$1 AND bot_number=$2", phone, from_bot_number)
            if row:
                await conn.execute("""
                    INSERT INTO carts (phone, bot_number, cart_data, updated_at)
                    VALUES ($1, $2, $3::jsonb, NOW())
                    ON CONFLICT (phone, bot_number) DO UPDATE SET cart_data=EXCLUDED.cart_data, updated_at=NOW()
                """, phone, to_bot_number, row["cart_data"])
                await conn.execute("DELETE FROM carts WHERE phone=$1 AND bot_number=$2", phone, from_bot_number)

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

async def db_get_tables(branch_id: int = None, is_main: bool = False):
    """
    Devuelve las mesas.
    Si is_main es True, trae SOLO las mesas de la matriz (branch_id IS NULL).
    Si branch_id tiene un número, trae SOLO las de esa sucursal.
    Si ambas son False/None, trae TODAS las mesas.
    """
    pool = await get_pool()
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
    pool = await get_pool()
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
    
    pool = await get_pool()
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

async def db_mark_factura_generada(base_order_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE table_orders SET status='factura_generada', updated_at=NOW() "
            "WHERE (id=$1 OR base_order_id=$1) AND status NOT IN ('cancelado','factura_entregada')",
            base_order_id
        )

async def db_get_first_table_order(base_order_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT phone, table_id, status FROM table_orders "
            "WHERE (id=$1 OR base_order_id=$1) ORDER BY created_at ASC LIMIT 1",
            base_order_id
        )
    return _serialize(dict(row)) if row else None

async def db_cleanup_after_checkout(phone: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM conversations WHERE phone=$1", phone)
        await conn.execute("DELETE FROM carts WHERE phone=$1", phone)

async def db_get_open_session_by_phone(phone: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM table_sessions WHERE phone=$1 AND closed_at IS NULL ORDER BY started_at DESC LIMIT 1",
            phone
        )
    return _serialize(dict(row)) if row else None

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

async def db_mark_session_nps_pending(phone: str, bot_number: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE table_sessions SET status='nps_pending', closed_by='factura_entregada', last_activity=NOW() "
            "WHERE phone=$1 AND bot_number=$2 AND status='active'",
            phone, bot_number
        )

async def db_close_session(phone: str, bot_number: str, reason: str = "manual", closed_by_username: str = "") -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE table_sessions
            SET status='closed', closed_at=NOW(), closed_by=$3, closed_by_username=$4,
                summary=jsonb_build_object('close_reason',$3::text,'closed_by_user',$4::text)
            WHERE phone=$1 AND bot_number=$2 AND status IN ('active','nps_pending') RETURNING *
        """, phone, bot_number, reason, closed_by_username)
        return _serialize(dict(row)) if row else None

async def db_mark_session_warned(session_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # El AND inactivity_warned=FALSE asegura que solo 1 worker pueda hacer el UPDATE
        result = await conn.execute(
            "UPDATE table_sessions SET inactivity_warned=TRUE WHERE id=$1 AND inactivity_warned=FALSE", 
            session_id
        )
        return result == "UPDATE 1"

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
        rows = await conn.fetch("""
            SELECT * FROM table_sessions WHERE
            (status='active' AND inactivity_warned=TRUE AND last_activity < NOW() - INTERVAL '5 minutes')
            OR (status='nps_pending' AND last_activity < NOW() - INTERVAL '5 minutes')
        """)
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
 
async def db_save_nps_response(phone: str, bot_number: str, score: int, comment: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Inferimos el branch_id buscando la mesa donde acaba de comer el cliente
        branch_id = await conn.fetchval("""
            SELECT rt.branch_id FROM table_sessions ts 
            JOIN restaurant_tables rt ON ts.table_id = rt.id 
            WHERE ts.phone = $1 ORDER BY ts.started_at DESC LIMIT 1
        """, phone)
        
        # 2. Guardamos la calificación amarrada a esa sucursal
        await conn.execute("""
            INSERT INTO nps_responses (phone, bot_number, score, comment, branch_id, created_at)
            VALUES ($1, $2, $3, $4, $5, NOW())
        """, phone, bot_number, score, comment, branch_id)
 
 
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


async def db_get_nps_stats(bot_number: str, period: str = "month", branch_id: int | str = None) -> dict:
    pool = await get_pool()
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    interval_str = period_map.get(period, "30 days")
    
    async with pool.acquire() as conn:
        conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
        params = [bot_number]
        
        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass
        elif branch_id is not None:
            conditions.append("branch_id = $2")
            params.append(branch_id)
        else:
            conditions.append("branch_id IS NULL")
            
        where_clause = " AND ".join(conditions)
        query = f"""
            SELECT COUNT(*) as total_responses, COALESCE(AVG(score), 0) as average_score,
            COUNT(*) FILTER (WHERE score = 5) as promoters, COUNT(*) FILTER (WHERE score = 4) as passives,
            COUNT(*) FILTER (WHERE score <= 3) as detractors
            FROM nps_responses WHERE {where_clause}
        """
        row = await conn.fetchrow(query, *params)
        
        total = row["total_responses"]
        nps_score = round(((row["promoters"] / total) - (row["detractors"] / total)) * 100) if total > 0 else 0
            
        return {
            "total_responses": total, "average_score": round(row["average_score"], 1),
            "nps_score": nps_score, "promoters": row["promoters"], "passives": row["passives"], "detractors": row["detractors"]
        }


async def db_get_nps_responses(bot_number: str, period: str = "month", limit: int = 50, branch_id: int | str = None) -> list:
    pool = await get_pool()
    period_map = {"today": "1 day", "week": "7 days", "month": "30 days", "semester": "180 days", "year": "365 days"}
    interval_str = period_map.get(period, "30 days")

    async with pool.acquire() as conn:
        conditions = ["bot_number = $1", f"created_at >= NOW() - INTERVAL '{interval_str}'"]
        params = [bot_number]
        
        # 🛡️ LA MAGIA DEL "ALL"
        if branch_id == "all":
            pass
        elif branch_id is not None:
            conditions.append("branch_id = $2")
            params.append(branch_id)
        else:
            conditions.append("branch_id IS NULL")
            
        where_clause = " AND ".join(conditions)
        limit_idx = len(params) + 1
        params.append(limit)

        query = f"SELECT * FROM nps_responses WHERE {where_clause} ORDER BY created_at DESC LIMIT ${limit_idx}"
        rows = await conn.fetch(query, *params)
        
        result = []
        for r in rows:
            d = dict(r)
            if d.get("created_at"): d["created_at"] = d["created_at"].isoformat() + "Z"
            result.append(d)
        return result


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
            await _sync_dish_availability(linked_dishes, False, restaurant_id)
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
 
        new_stock = fields.get("current_stock", existing["current_stock"])
        dishes    = fields.get("linked_dishes", existing["linked_dishes"])
        if isinstance(dishes, str):
            dishes = json.loads(dishes)
            
        restaurant_id = existing["restaurant_id"]
        
        await _sync_dish_availability(dishes, new_stock > 0, restaurant_id)
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
        await _sync_dish_availability(dishes, float(item["current_stock"]) > 0, restaurant_id)
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
                                await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)

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
                            await _sync_dish_availability_conn(conn, dishes, False, restaurant_id)
 
 
async def _sync_dish_availability_conn(conn, dish_names: list, available: bool, restaurant_id: int):
    """Activa o desactiva platos usando una conexión existente (dentro de transacción)."""
    for name in dish_names:
        await conn.execute(
            """INSERT INTO menu_availability (dish_name, restaurant_id, available, updated_at)
               VALUES ($1, $2, $3, NOW())
               ON CONFLICT (dish_name, restaurant_id)
               DO UPDATE SET available = EXCLUDED.available, updated_at = NOW()""",
            name, restaurant_id, available
        )

async def _sync_dish_availability(dish_names: list, available: bool, restaurant_id: int):
    """Activa o desactiva platos en menu_availability según el stock."""
    if not dish_names:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _sync_dish_availability_conn(conn, dish_names, available, restaurant_id)


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

    # asyncpg espera date/time nativos, no strings
    raw_date = data.get("issue_date")
    issue_date = (
        datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        if isinstance(raw_date, str) else raw_date
    )

    raw_time = data.get("issue_time")
    issue_time = (
        datetime.strptime(raw_time[:8], "%H:%M:%S").time()
        if isinstance(raw_time, str) else raw_time
    )

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
            issue_date, issue_time,
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
# SUBSCRIPTION USAGE — consumo diario de tokens y facturas
# ══════════════════════════════════════════════════════════════════════

_usage_table_ensured = False

async def _ensure_usage_table() -> None:
    """Crea subscription_usage si no existe (DDL idempotente, ejecuta una vez por proceso)."""
    global _usage_table_ensured
    if _usage_table_ensured:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscription_usage (
                id             BIGSERIAL   PRIMARY KEY,
                restaurant_id  INTEGER     NOT NULL,
                usage_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
                total_tokens   INTEGER     NOT NULL DEFAULT 0,
                total_invoices INTEGER     NOT NULL DEFAULT 0,
                updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (restaurant_id, usage_date)
            )
        """)
    _usage_table_ensured = True


async def db_increment_token_usage(restaurant_id: int, tokens: int) -> None:
    """Suma `tokens` al contador diario del restaurante (upsert atómico)."""
    if tokens <= 0:
        return
    await _ensure_usage_table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO subscription_usage (restaurant_id, usage_date, total_tokens)
               VALUES ($1, CURRENT_DATE, $2)
               ON CONFLICT (restaurant_id, usage_date) DO UPDATE
               SET total_tokens = subscription_usage.total_tokens + $2,
                   updated_at   = NOW()""",
            restaurant_id, tokens,
        )


async def db_increment_invoice_usage(restaurant_id: int) -> None:
    """Incrementa en 1 el contador de facturas diarias del restaurante (upsert atómico)."""
    await _ensure_usage_table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO subscription_usage (restaurant_id, usage_date, total_invoices)
               VALUES ($1, CURRENT_DATE, 1)
               ON CONFLICT (restaurant_id, usage_date) DO UPDATE
               SET total_invoices = subscription_usage.total_invoices + 1,
                   updated_at     = NOW()""",
            restaurant_id,
        )


async def db_check_usage_limits(restaurant_id: int) -> None:
    """
    Verifica que el restaurante no haya superado sus límites diarios.
    Lee restaurants.features.plan_limits → { daily_tokens, daily_invoices }.
    Si plan_limits está ausente, no se aplica ningún límite.
    Lanza UsageLimitExceeded si se superó algún límite.
    """
    await _ensure_usage_table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Leer límites del plan desde features
        row = await conn.fetchrow(
            "SELECT features FROM restaurants WHERE id = $1", restaurant_id
        )
        if not row:
            return
        feats = row["features"] or {}
        if isinstance(feats, str):
            try:
                feats = json.loads(feats)
            except Exception:
                feats = {}
        limits = feats.get("plan_limits") if isinstance(feats, dict) else None
        if not limits:
            return  # sin límites configurados → acceso libre

        # Leer consumo del día actual
        usage = await conn.fetchrow(
            """SELECT total_tokens, total_invoices
               FROM subscription_usage
               WHERE restaurant_id = $1 AND usage_date = CURRENT_DATE""",
            restaurant_id,
        )
        used_tokens   = usage["total_tokens"]   if usage else 0
        used_invoices = usage["total_invoices"] if usage else 0

        token_limit   = limits.get("daily_tokens")
        invoice_limit = limits.get("daily_invoices")

        if token_limit and used_tokens >= int(token_limit):
            raise UsageLimitExceeded("tokens", used_tokens, int(token_limit))
        if invoice_limit and used_invoices >= int(invoice_limit):
            raise UsageLimitExceeded("facturas", used_invoices, int(invoice_limit))


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
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, "
            "created_at, updated_at FROM staff "
            "WHERE restaurant_id=$1 ORDER BY name ASC",
            restaurant_id,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_team_staff_by_branch(restaurant_id: int) -> list:
    """Return staff formatted for the Mi Equipo unified team view."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, name, role, roles, phone, active "
            "FROM staff WHERE restaurant_id=$1 ORDER BY name ASC",
            restaurant_id,
        )
    result = []
    for r in rows:
        d = dict(r)
        roles_list = d.get("roles") or []
        if not roles_list and d.get("role"):
            roles_list = [d["role"]]
        d["roles"] = roles_list
        d["source"] = "staff"
        d["branch_id"] = restaurant_id
        result.append(d)
    return result


async def db_get_staff_for_pin_login(restaurant_id: int, name: str) -> dict | None:
    """Return a staff member's record including pin hash for PIN authentication."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, pin "
            "FROM staff WHERE restaurant_id=$1 AND LOWER(name)=LOWER($2) AND active=true",
            restaurant_id, name,
        )
    if not row:
        return None
    d = dict(row)
    roles_list = d.get("roles") or []
    if not roles_list and d.get("role"):
        roles_list = [d["role"]]
    d["roles"] = roles_list
    return d


async def db_get_staff_candidates_by_name(name: str) -> list:
    """Retorna todos los staff activos con ese nombre (multi-restaurante).
    El caller verifica el PIN contra cada candidato para resolver colisiones."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text, restaurant_id, name, role, roles, active, phone, pin "
            "FROM staff WHERE LOWER(name)=LOWER($1) AND active=true "
            "ORDER BY restaurant_id",
            name,
        )
    result = []
    for row in rows:
        d = dict(row)
        roles_list = d.get("roles") or []
        if not roles_list and d.get("role"):
            roles_list = [d["role"]]
        d["roles"] = roles_list
        result.append(d)
    return result


async def db_create_staff(
    restaurant_id: int,
    name: str,
    role: str,
    pin_hash: str,
    phone: str = "",
    roles: list = None,
) -> dict:
    """Insert a new staff member. Returns the created row."""
    if roles is None:
        roles = [role] if role else []
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff (restaurant_id, name, role, pin, phone, roles)
               VALUES ($1, $2, $3, $4, $5, $6::jsonb)
               RETURNING id::text, restaurant_id, name, role, roles, active, phone,
                         created_at, updated_at""",
            restaurant_id, name, role, pin_hash, phone, json.dumps(roles),
        )
    return _serialize(dict(row))


async def db_update_staff(staff_id: str, restaurant_id: int, fields: dict) -> dict | None:
    """
    Update mutable staff fields (name, role, roles, pin, phone, active).
    Ignores unknown keys. Returns updated row or None if not found.
    Only updates columns that are explicitly passed in fields.
    All values are passed as parameters — no f-string SQL.
    """
    allowed = {"name", "role", "roles", "pin", "phone", "active"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return None

    # Serialize roles list to JSON string for JSONB column
    if "roles" in updates and isinstance(updates["roles"], list):
        updates["roles"] = json.dumps(updates["roles"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Build SET clause with positional params starting at $3
        set_parts = []
        values = []
        for i, (col, val) in enumerate(updates.items(), start=3):
            cast = "::jsonb" if col == "roles" else ""
            set_parts.append(f"{col}=${i}{cast}")
            values.append(val)

        sql = (
            f"UPDATE staff SET {', '.join(set_parts)}, updated_at=NOW() "
            f"WHERE id=$1::uuid AND restaurant_id=$2 "
            f"RETURNING id::text, restaurant_id, name, role, roles, active, phone, "
            f"created_at, updated_at"
        )
        row = await conn.fetchrow(sql, staff_id, restaurant_id, *values)
    return _serialize(dict(row)) if row else None


async def db_delete_staff(staff_id: str, restaurant_id: int) -> bool:
    """Elimina permanentemente un miembro de staff. Retorna True si se eliminó."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM staff WHERE id=$1::uuid AND restaurant_id=$2",
            staff_id, restaurant_id,
        )
    return result.split()[-1] != "0"  # "DELETE N" → True si N > 0


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


# ══════════════════════════════════════════════════════════════════════
# LOYALTY — Modelo Ledger
# ══════════════════════════════════════════════════════════════════════
#
# Modelo de dos tablas:
#   loyalty_customers  — balance actual exacto por (restaurant_id, phone). O(1) reads.
#   loyalty_ledger     — registro inmutable de cada movimiento (+acumulación / -canje).
#
# Config por restaurante (restaurants.features JSONB):
#   "loyalty_points_per_1k"  : puntos ganados por cada $1,000 COP pagados (default: 1)
#   "loyalty_point_value_cop": valor en COP de cada punto al canjear        (default: 10)
# ══════════════════════════════════════════════════════════════════════

_loyalty_tables_ensured = False


async def _ensure_loyalty_tables() -> None:
    global _loyalty_tables_ensured
    if _loyalty_tables_ensured:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS loyalty_customers (
                id             SERIAL PRIMARY KEY,
                restaurant_id  INTEGER NOT NULL,
                phone          TEXT    NOT NULL,
                points_balance INTEGER NOT NULL DEFAULT 0,
                total_earned   INTEGER NOT NULL DEFAULT 0,
                total_redeemed INTEGER NOT NULL DEFAULT 0,
                created_at     TIMESTAMP DEFAULT NOW(),
                updated_at     TIMESTAMP DEFAULT NOW(),
                UNIQUE (restaurant_id, phone)
            );
            CREATE TABLE IF NOT EXISTS loyalty_ledger (
                id            BIGSERIAL PRIMARY KEY,
                restaurant_id INTEGER NOT NULL,
                phone         TEXT    NOT NULL,
                delta         INTEGER NOT NULL,
                reason        TEXT    NOT NULL DEFAULT 'purchase',
                order_id      TEXT,
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        for idx in [
            "CREATE INDEX IF NOT EXISTS idx_loyalty_cust_lookup   ON loyalty_customers (restaurant_id, phone)",
            "CREATE INDEX IF NOT EXISTS idx_loyalty_ledger_lookup  ON loyalty_ledger    (restaurant_id, phone, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_loyalty_ledger_order   ON loyalty_ledger    (order_id) WHERE order_id IS NOT NULL",
        ]:
            try:
                await conn.execute(idx)
            except Exception:
                pass
    _loyalty_tables_ensured = True


async def _loyalty_cfg(conn, restaurant_id: int) -> dict:
    """
    Lee loyalty_points_per_1k y loyalty_point_value_cop de restaurants.features.
    Devuelve defaults seguros si no están configurados.
    """
    row = await conn.fetchrow(
        "SELECT features FROM restaurants WHERE id=$1", restaurant_id
    )
    feats = (row["features"] or {}) if row else {}
    if isinstance(feats, str):
        try:
            feats = json.loads(feats)
        except Exception:
            feats = {}
    return {
        "points_per_1k":   max(1, int(feats.get("loyalty_points_per_1k", 1))),
        "point_value_cop": max(1, int(feats.get("loyalty_point_value_cop", 10))),
    }


async def db_get_loyalty_balance(restaurant_id: int, phone: str) -> dict | None:
    """
    Consulta O(1) del saldo. El bot la consume como herramienta ultra-ligera.
    Retorna {"puntos_actuales": N, "equivalencia_cop": N*point_value} o None si
    el cliente no tiene registro de fidelización.
    """
    await _ensure_loyalty_tables()
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT points_balance FROM loyalty_customers WHERE restaurant_id=$1 AND phone=$2",
            restaurant_id, _normalize_phone(phone),
        )
        if not row:
            return None
        cfg = await _loyalty_cfg(conn, restaurant_id)
        pts = row["points_balance"]
    return {"puntos_actuales": pts, "equivalencia_cop": pts * cfg["point_value_cop"]}


async def db_accrue_loyalty_points(
    restaurant_id: int,
    phone: str,
    order_id: str,
    total_cop: float,
) -> int:
    """
    Calcula y acumula puntos por una compra pagada. Idempotente: si ya existe
    una entrada positiva en el ledger para este order_id, no duplica.
    Retorna los puntos acumulados (0 si ya estaba procesado).
    """
    await _ensure_loyalty_tables()
    clean_phone = _normalize_phone(phone)
    pool = await get_pool()
    async with pool.acquire() as conn:
        cfg = await _loyalty_cfg(conn, restaurant_id)
        points = max(1, int(total_cop / 1000) * cfg["points_per_1k"])
        # Idempotencia: verificar si ya se procesó este order_id
        existing = await conn.fetchval(
            "SELECT id FROM loyalty_ledger WHERE restaurant_id=$1 AND order_id=$2 AND delta > 0 LIMIT 1",
            restaurant_id, order_id,
        )
        if existing:
            return 0
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO loyalty_ledger (restaurant_id, phone, delta, reason, order_id)
                   VALUES ($1, $2, $3, 'purchase', $4)""",
                restaurant_id, clean_phone, points, order_id,
            )
            await conn.execute(
                """INSERT INTO loyalty_customers (restaurant_id, phone, points_balance, total_earned)
                   VALUES ($1, $2, $3, $3)
                   ON CONFLICT (restaurant_id, phone) DO UPDATE
                   SET points_balance = loyalty_customers.points_balance + $3,
                       total_earned   = loyalty_customers.total_earned   + $3,
                       updated_at     = NOW()""",
                restaurant_id, clean_phone, points,
            )
    return points


async def db_redeem_loyalty_points(
    restaurant_id: int,
    phone: str,
    points: int,
    order_id: str,
) -> dict:
    """
    Canjea puntos contra una compra. Bloquea la fila con FOR UPDATE para
    evitar race conditions en entornos multi-worker.
    Retorna {"redeemed": N, "cop_discount": N*point_value, "new_balance": M}.
    Lanza ValueError si el saldo es insuficiente.
    """
    await _ensure_loyalty_tables()
    if points <= 0:
        raise ValueError("Los puntos a canjear deben ser positivos")
    clean_phone = _normalize_phone(phone)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT points_balance FROM loyalty_customers "
                "WHERE restaurant_id=$1 AND phone=$2 FOR UPDATE",
                restaurant_id, clean_phone,
            )
            current = row["points_balance"] if row else 0
            if current < points:
                raise ValueError(
                    f"Saldo insuficiente: {current} puntos disponibles, "
                    f"se intentaron canjear {points}"
                )
            await conn.execute(
                """INSERT INTO loyalty_ledger (restaurant_id, phone, delta, reason, order_id)
                   VALUES ($1, $2, $3, 'redeem', $4)""",
                restaurant_id, clean_phone, -points, order_id,
            )
            new_balance = await conn.fetchval(
                """UPDATE loyalty_customers
                   SET points_balance = points_balance  - $3,
                       total_redeemed = total_redeemed  + $3,
                       updated_at     = NOW()
                   WHERE restaurant_id=$1 AND phone=$2
                   RETURNING points_balance""",
                restaurant_id, clean_phone, points,
            )
            cfg = await _loyalty_cfg(conn, restaurant_id)
    return {
        "redeemed":     points,
        "cop_discount": points * cfg["point_value_cop"],
        "new_balance":  new_balance,
    }


async def db_adjust_loyalty_points(
    restaurant_id: int,
    phone: str,
    delta: int,
    reason: str,
) -> dict:
    """
    Ajuste manual (admin). delta puede ser positivo o negativo.
    No permite dejar el saldo en negativo.
    """
    await _ensure_loyalty_tables()
    clean_phone = _normalize_phone(phone)
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if delta < 0:
                row = await conn.fetchrow(
                    "SELECT points_balance FROM loyalty_customers "
                    "WHERE restaurant_id=$1 AND phone=$2 FOR UPDATE",
                    restaurant_id, clean_phone,
                )
                current = row["points_balance"] if row else 0
                if current + delta < 0:
                    raise ValueError(
                        f"El ajuste dejaría el saldo negativo "
                        f"({current} + {delta} = {current + delta})"
                    )
            await conn.execute(
                """INSERT INTO loyalty_ledger (restaurant_id, phone, delta, reason)
                   VALUES ($1, $2, $3, $4)""",
                restaurant_id, clean_phone, delta, reason[:100],
            )
            new_balance = await conn.fetchval(
                """INSERT INTO loyalty_customers
                       (restaurant_id, phone, points_balance, total_earned, total_redeemed)
                   VALUES ($1, $2, GREATEST(0, $3), GREATEST(0, $3), 0)
                   ON CONFLICT (restaurant_id, phone) DO UPDATE
                   SET points_balance = GREATEST(0, loyalty_customers.points_balance + $3),
                       total_earned   = CASE WHEN $3 > 0
                                        THEN loyalty_customers.total_earned + $3
                                        ELSE loyalty_customers.total_earned END,
                       total_redeemed = CASE WHEN $3 < 0
                                        THEN loyalty_customers.total_redeemed + (-$3)
                                        ELSE loyalty_customers.total_redeemed END,
                       updated_at     = NOW()
                   RETURNING points_balance""",
                restaurant_id, clean_phone, delta,
            )
    return {"new_balance": new_balance}


async def db_get_loyalty_ledger(
    restaurant_id: int,
    phone: str,
    limit: int = 50,
) -> list[dict]:
    """Historial de movimientos de un cliente (para dashboard / POS)."""
    await _ensure_loyalty_tables()
    limit = min(limit, 200)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, delta, reason, order_id, created_at
               FROM loyalty_ledger
               WHERE restaurant_id=$1 AND phone=$2
               ORDER BY created_at DESC
               LIMIT $3""",
            restaurant_id, _normalize_phone(phone), limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_loyalty_stats(restaurant_id: int, limit: int = 100) -> list[dict]:
    """Top clientes ordenados por saldo (para dashboard de fidelización)."""
    await _ensure_loyalty_tables()
    limit = min(limit, 500)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT phone, points_balance, total_earned, total_redeemed, updated_at
               FROM loyalty_customers
               WHERE restaurant_id=$1
               ORDER BY points_balance DESC
               LIMIT $2""",
            restaurant_id, limit,
        )
    return [_serialize(dict(r)) for r in rows]


async def db_get_phone_for_base_order(base_order_id: str) -> str | None:
    """
    Obtiene el teléfono del cliente asociado a un ticket de mesa.
    Busca en table_orders por id directo o por base_order_id de sub-órdenes.
    Usado por el background task de acumulación de loyalty en caja.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        phone = await conn.fetchval(
            "SELECT phone FROM table_orders WHERE id=$1 OR base_order_id=$1 LIMIT 1",
            base_order_id,
        )
    return phone


async def db_is_duplicate_wam(wam_id: str) -> bool:
    """
    Deduplicación idempotente por WAM_ID (WhatsApp Message ID).

    Lógica:
    - Borra entradas con más de 2 minutos (ventana de reintentos de Meta).
    - Intenta insertar el wam_id con INSERT ... ON CONFLICT DO NOTHING.
    - Si el INSERT no devuelve filas → el ID ya existía → duplicado (True).
    - Si el INSERT devuelve el wam_id → era nuevo → procesar (False).

    Al usar la PK como única constraint, el INSERT es atómico y safe
    bajo concurrencia multi-worker sin necesidad de locks adicionales.
    """
    if not wam_id:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM processed_wam_ids WHERE received_at < NOW() - INTERVAL '2 minutes'"
        )
        result = await conn.fetchval(
            "INSERT INTO processed_wam_ids (wam_id) VALUES ($1) ON CONFLICT DO NOTHING RETURNING wam_id",
            wam_id,
        )
        # result is None  → row already existed → duplicate
        # result is wam_id → freshly inserted   → new message
        return result is None