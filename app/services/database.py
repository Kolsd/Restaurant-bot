import os
import asyncpg
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from app.services.money import to_decimal, money_mul, money_sum, quantize_money, ZERO


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
    # Datetimes → ISO string. All other types (including Decimal from NUMERIC columns)
    # pass through unchanged. Decimal→float conversion happens at the JSON boundary
    # (float(...) calls) in the repo layer, not here. Keeping Decimal intact preserves
    # precision for any further arithmetic done between _serialize and the response.
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


# db_init_nps_inventory re-exported below alongside the rest of the inventory module (Fase 6)

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

# === Conversations: moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_get_history,
    db_save_history,
    db_get_all_conversations,
    db_delete_conversation,
    db_get_conversation_details,
    db_toggle_bot,
    db_cleanup_old_conversations,
)


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
            json.dumps(menu_data), restaurant_id
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
    import hashlib as _hashlib
    pool = await get_pool()
    async with pool.acquire() as conn:
        expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
        token_hash = _hashlib.sha256(token.encode()).digest()
        try:
            # Store both hash and plaintext so sessions_repo.get_session finds
            # pin_login sessions via Phase 1 (hash lookup), not just the legacy fallback.
            await conn.execute(
                "INSERT INTO sessions (token, token_hash, username, expires_at) VALUES ($1, $2, $3, $4)",
                token, token_hash, username, expires
            )
        except Exception:
            # Fallback: migration 0009 not yet applied in this environment.
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

async def db_cleanup_expired_sessions():
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM sessions WHERE expires_at < NOW()")
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            print(f"🧹 Sesiones expiradas eliminadas: {count}", flush=True)


# === Conversations (carts): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_get_cart,
    db_save_cart,
    db_clear_cart,
    db_migrate_cart,
)

# === Tables/POS: moved to app.repositories.tables_repo (Fase 6) ===
from app.repositories.tables_repo import (
    db_init_tables,
    db_get_tables,
    db_create_table,
    db_auto_create_table,
    db_delete_table,
    db_get_table_by_id,
    db_save_table_order,
    db_get_base_order_status,
    db_merge_table_order_items,
    db_get_table_orders,
    db_update_table_order_status,
    db_get_base_order_id,
    db_get_next_sub_number,
    db_get_table_bill,
    db_close_table_bill,
    db_mark_factura_generada,
    db_get_first_table_order,
    db_cleanup_after_checkout,
    db_get_open_session_by_phone,
    db_has_pending_invoice,
    db_get_active_table_order,
    db_init_waiter_alerts,
    db_create_waiter_alert,
    db_get_waiter_alerts,
    db_dismiss_waiter_alert,
    db_init_table_sessions,
    db_get_active_session,
    db_create_table_session,
    db_touch_session,
    db_touch_session_with_phone_id,
    db_session_mark_order,
    db_session_mark_delivered,
    db_mark_session_nps_pending,
    db_close_session,
    db_mark_session_warned,
    db_get_stale_sessions,
    db_get_closeable_sessions,
    db_get_closed_sessions,
    db_get_session_by_id,
    db_reopen_session,
)

async def db_get_restaurant_settings() -> dict:
    all_r = await db_get_all_restaurants()
    return all_r[0] if all_r else {}
 
# === Conversations (NPS per-conversation state): moved to app.repositories.conversations_repo (Fase 6) ===
# Restaurant-wide NPS analytics (db_get_nps_stats, db_get_nps_responses) remain below.
from app.repositories.conversations_repo import (
    db_save_nps_response,
    db_save_nps_pending,
    db_update_nps_comment,
    db_get_pending_nps_score,
)


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


# === Conversations (NPS waiting state): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import (
    db_save_nps_waiting,
    db_get_nps_waiting,
    db_clear_nps_waiting,
)


# === Inventory: moved to app.repositories.inventory_repo (Fase 6) ===
from app.repositories.inventory_repo import (
    db_init_nps_inventory,
    db_get_inventory,
    db_create_inventory_item,
    db_update_inventory_item,
    db_delete_inventory_item,
    db_adjust_inventory_stock,
    db_get_inventory_history,
    db_get_inventory_alerts,
    db_deduct_inventory_for_order,
    db_init_dish_recipes,
    db_upsert_dish_recipe,
    db_get_dish_recipe,
    db_get_all_recipes,
    db_delete_dish_recipe,
    db_get_food_costs,
    _sync_dish_availability,
    _sync_dish_availability_conn,
)


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


# === Tables/POS — split checks + ticket data: moved to app.repositories.tables_repo (Fase 6) ===
from app.repositories.tables_repo import (
    db_get_order_ticket_data,
    db_init_table_checks,
    db_create_checks,
    db_get_checks,
    db_get_check,
    db_finalize_check_payment,
    db_delete_open_check,
    db_attach_proposal,
    db_set_check_tip,
    db_attach_proof,
    db_get_open_proposal_for_phone,
    db_list_checkout_proposals,
    db_get_check_ticket,
)


# === Staff: moved to app.repositories.staff_repo (Fase 6) ===
from app.repositories.staff_repo import (
    db_get_staff,
    db_get_team_staff_by_branch,
    db_get_staff_for_pin_login,
    db_get_staff_candidates_by_name,
    db_create_staff,
    db_update_staff,
    db_delete_staff,
    _record_attendance_deduction,
    db_clock_in,
    db_clock_out,
    db_get_open_shifts,
    db_get_shifts,
    db_calculate_tip_pool,
    db_calculate_tips_by_attendance,
    db_save_tip_distribution,
    db_get_tip_distributions,
)


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


# === Conversations (WAM deduplication): moved to app.repositories.conversations_repo (Fase 6) ===
from app.repositories.conversations_repo import db_is_duplicate_wam


# === Staff (advanced): moved to app.repositories.staff_repo (Fase 6) ===
from app.repositories.staff_repo import (
    db_save_webauthn_credential,
    db_get_webauthn_credentials_by_staff,
    db_get_webauthn_credentials_by_restaurant,
    db_get_webauthn_credential,
    db_update_webauthn_sign_count,
    db_delete_webauthn_credential,
    db_save_webauthn_challenge,
    db_consume_webauthn_challenge,
    db_cleanup_expired_challenges,
    db_start_break,
    db_end_break,
    db_get_breaks_for_shift,
    db_get_open_break,
    db_upsert_schedule,
    db_bulk_upsert_schedules,
    db_get_schedules,
    db_delete_schedule,
    db_edit_shift,
    db_get_timecard,
    db_get_overtime_report,
    db_get_attendance_report,
    db_list_deduction_items,
    db_create_deduction_item,
    db_update_deduction_item,
    db_delete_deduction_item,
    db_calculate_payroll,
    db_save_payroll_run,
    db_get_payroll_runs,
    db_get_payroll_run,
    db_approve_payroll_run,
    db_list_contract_templates,
    db_create_contract_template,
    db_update_contract_template,
    db_delete_contract_template,
    db_assign_staff_contract,
    db_list_overtime_requests,
    db_upsert_overtime_request,
    db_review_overtime_request,
)
