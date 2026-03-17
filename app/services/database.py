import os
import asyncpg
import json
from datetime import date, datetime

_pool = None


def _normalize_phone(number: str) -> str:
    """
    Normaliza números de teléfono/WhatsApp para búsquedas en DB.
    - Elimina espacios
    - Elimina '+' inicial
    """
    if not number:
        return ""
    return number.replace(" ", "").replace("+", "")


def _to_date(s: str) -> date:
    """Convierte string YYYY-MM-DD a objeto date de Python."""
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
            raise RuntimeError("DATABASE_URL no está configurada")
        database_url = database_url.replace("postgres://", "postgresql://", 1)
        _pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=5,
            init=lambda conn: conn.set_type_codec(
                'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
            )
        )
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. Creamos las tablas básicas si no existen
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS restaurants (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                whatsapp_number TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL DEFAULT '',
                menu JSONB NOT NULL DEFAULT '{}'::jsonb,
                subscription_status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS reservations (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                guests INTEGER NOT NULL,
                phone TEXT NOT NULL,
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
                created_at TIMESTAMP DEFAULT NOW(),
                paid_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT NOT NULL,
                bot_number TEXT NOT NULL DEFAULT '',
                history JSONB NOT NULL DEFAULT '[]',
                bot_paused BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                restaurant_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # 2. Forzamos a que las columnas nuevas existan por si usamos una base de datos vieja
        try:
            await conn.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS bot_paused BOOLEAN DEFAULT FALSE;")
            await conn.execute("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS bot_number TEXT NOT NULL DEFAULT '';")
            await conn.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_pkey;")
            await conn.execute("ALTER TABLE conversations ADD CONSTRAINT conversations_pkey PRIMARY KEY (phone, bot_number);")
            await conn.execute("ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'active';")
            await conn.execute("ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS whatsapp_number TEXT UNIQUE;")
            await conn.execute("ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS address TEXT DEFAULT '';")
        except Exception as e:
            print(f"Aviso al parchear tablas: {e}")

        # 3. Usuario demo por defecto
        import hashlib
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name)
                VALUES ($1,$2,$3) ON CONFLICT (username) DO NOTHING;
            """, "demo@restaurante.com", hashlib.sha256("demo123".encode()).hexdigest(), "La Trattoria Italiana")
        except Exception as e:
            print(f"Aviso al crear usuario demo: {e}")
            
    print("✅ Base de datos inicializada de forma segura")

# ─────────────────────────────────────────────
# RESERVACIONES
# ─────────────────────────────────────────────

async def db_add_reservation(name, date_str, time, guests, phone, notes=""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO reservations (name, date, time, guests, phone, notes)
            VALUES ($1,$2,$3,$4,$5,$6) RETURNING *
        """, name, date_str, time, int(guests), phone, notes)
        return _serialize(dict(row))


async def db_get_reservations_today(date_str: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reservations WHERE date=$1 ORDER BY time", date_str)
        return [_serialize(dict(r)) for r in rows]


async def db_get_reservations_range(date_from: str, date_to: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM reservations
            WHERE date >= $1 AND date <= $2 ORDER BY date, time
        """, date_from, date_to)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_reservations():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM reservations ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]


# ─────────────────────────────────────────────
# ÓRDENES
# ─────────────────────────────────────────────

async def db_save_order(order: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO orders (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, payment_url)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (id) DO UPDATE SET
                status=EXCLUDED.status, paid=EXCLUDED.paid,
                payment_url=EXCLUDED.payment_url
        """,
        order["id"], order["phone"], json.dumps(order["items"]),
        order["order_type"], order.get("address",""), order.get("notes",""),
        order["subtotal"], order["delivery_fee"], order["total"],
        order["status"], order["paid"], order.get("payment_url",""))


async def db_confirm_payment(order_id: str, transaction_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE orders SET paid=TRUE, status='confirmado',
                transaction_id=$2, paid_at=NOW()
            WHERE id=$1 RETURNING *
        """, order_id, transaction_id)
        return _serialize(dict(row)) if row else None


async def db_get_orders_range(date_from: str, date_to: str):
    pool = await get_pool()
    d_from = _to_date(date_from)
    # Add 1 day to d_to so we capture the full day including late UTC times
    from datetime import timedelta
    d_to_inclusive = _to_date(date_to) + timedelta(days=1)
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM orders
            WHERE created_at >= $1 AND created_at < $2
            ORDER BY created_at DESC
        """, d_from, d_to_inclusive)
        return [_serialize(dict(r)) for r in rows]


async def db_get_order(order_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM orders WHERE id=$1", order_id)
        return _serialize(dict(row)) if row else None


async def db_get_all_orders():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM orders ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]


# ─────────────────────────────────────────────
# CONVERSACIONES
# ─────────────────────────────────────────────

async def db_get_history(phone: str, bot_number: str) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
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


async def db_get_all_conversations():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT phone, history, updated_at FROM conversations ORDER BY updated_at DESC")
        result = []
        for r in rows:
            history = r["history"] if isinstance(r["history"], list) else json.loads(r["history"])
            last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
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


# ─────────────────────────────────────────────
# USUARIOS
# ─────────────────────────────────────────────

async def db_get_user(username: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE username=$1", username.lower().strip())
        return dict(row) if row else None


async def db_get_restaurant_by_phone(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(whatsapp_number.strip()),
        )
        return _serialize(dict(row)) if row else None


async def db_create_user(username: str, password_hash: str, restaurant_name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name)
                VALUES ($1,$2,$3)
            """, username.lower().strip(), password_hash, restaurant_name)
            return True
        except asyncpg.UniqueViolationError:
            return False


async def db_get_all_users():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT username, restaurant_name FROM users")
        return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# MENÚ — disponibilidad en DB
# ─────────────────────────────────────────────

async def db_get_menu_availability():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS menu_availability (
                dish_name TEXT PRIMARY KEY,
                available BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        rows = await conn.fetch("SELECT dish_name, available FROM menu_availability")
        return {r['dish_name']: r['available'] for r in rows}


async def db_set_dish_availability(dish_name: str, available: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO menu_availability (dish_name, available, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (dish_name) DO UPDATE
            SET available = EXCLUDED.available, updated_at = NOW()
        """, dish_name, available)


# ─────────────────────────────────────────────
# CONVERSACIONES — limpiar antiguas
# ─────────────────────────────────────────────

async def db_cleanup_old_conversations(days: int = 7):
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("""
            DELETE FROM conversations
            WHERE updated_at < NOW() - INTERVAL '7 days'
        """)
        return result


# ─────────────────────────────────────────────
# MESAS — QR Table Ordering
# ─────────────────────────────────────────────

async def db_init_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS restaurant_tables (
                id TEXT PRIMARY KEY,
                number INTEGER NOT NULL,
                name TEXT NOT NULL,
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


async def db_get_tables():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")
        return [_serialize(dict(r)) for r in rows]


async def db_create_table(table_id: str, number: int, name: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name)
            VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name
        """, table_id, number, name)


async def db_delete_table(table_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurant_tables SET active=FALSE WHERE id=$1", table_id)


async def db_save_table_order(order: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO table_orders (id, table_id, table_name, phone, items, status, notes, total)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items, status=EXCLUDED.status,
                notes=EXCLUDED.notes, total=EXCLUDED.total,
                updated_at=NOW()
        """, order['id'], order['table_id'], order['table_name'],
            order['phone'], json.dumps(order['items']),
            order.get('status', 'recibido'), order.get('notes', ''),
            order.get('total', 0))


async def db_get_table_orders(status: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch("""
                SELECT * FROM table_orders WHERE status=$1
                ORDER BY created_at DESC
            """, status)
        else:
            rows = await conn.fetch("""
                SELECT * FROM table_orders
                WHERE status NOT IN ('entregado','cancelado')
                ORDER BY created_at ASC
            """)
        result = []
        for r in rows:
            d = _serialize(dict(r))
            if isinstance(d['items'], str):
                d['items'] = json.loads(d['items'])
            result.append(d)
        return result


async def db_update_table_order_status(order_id: str, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1
        """, order_id, status)


async def db_get_table_by_id(table_id: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurant_tables WHERE id=$1", table_id)
        return _serialize(dict(row)) if row else None

async def db_create_restaurant(name: str, whatsapp_number: str, address: str, menu: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurants (name, whatsapp_number, address, menu)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (whatsapp_number) DO UPDATE
            SET name=EXCLUDED.name, address=EXCLUDED.address, menu=EXCLUDED.menu
        """, name, whatsapp_number, address, json.dumps(menu))


async def db_get_restaurant_by_bot_number(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(whatsapp_number),
        )
        return _serialize(dict(row)) if row else None


async def db_get_menu(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT menu FROM restaurants WHERE whatsapp_number=$1", whatsapp_number
        )
        if row and row['menu']:
            # menu is stored as JSONB, ensure it's a dict
            return row['menu'] if isinstance(row['menu'], dict) else json.loads(row['menu'])
        return None


async def db_get_top_dishes(whatsapp_number: str, top_n: int = 5):
    menu = await db_get_menu(whatsapp_number)
    if not menu:
        return []
    # Suponiendo que la estructura del menú contiene una lista de platos bajo la clave 'dishes' o similar,
    # y cada plato tiene al menos un campo 'popularity' o 'orders' para determinar los más destacados.
    dishes = menu.get('dishes', [])
    if not isinstance(dishes, list) or len(dishes) == 0:
        # Si el menú está plano, devolver los primeros 'top_n' elementos.
        if isinstance(menu, list):
            return menu[:top_n]
        return []
    # Ordenar por campo 'popularity' o 'orders' si existe, si no devolver los primeros top_n.
    if any('popularity' in d for d in dishes):
        dishes = sorted(dishes, key=lambda x: x.get('popularity', 0), reverse=True)
    elif any('orders' in d for d in dishes):
        dishes = sorted(dishes, key=lambda x: x.get('orders', 0), reverse=True)
    return dishes[:top_n]


# ─────────────────────────────────────────────
# ACTUALIZAR ESTADO DE SUSCRIPCIÓN — restaurants
# ─────────────────────────────────────────────

async def db_update_subscription(restaurant_id: int, new_status: str):
    """
    Actualiza el subscription_status de un restaurante dada su ID.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET subscription_status=$2 WHERE id=$1",
            restaurant_id, new_status
        )

# ─────────────────────────────────────────────
# BOT PAUSE/FUNCIONES PARA CONVERSACIONES
# ─────────────────────────────────────────────

async def db_get_conversation_details(phone: str, bot_number: str):
    """
    Retorna un dict con 'history' y 'bot_paused' para un número de teléfono.
    Si no existe el registro, retorna {'history': [], 'bot_paused': False}
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history, bot_paused FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number
        )
        if row:
            history = row["history"] if isinstance(row["history"], list) else json.loads(row["history"])
            bot_paused = row["bot_paused"] if row["bot_paused"] is not None else False
            return {"history": history, "bot_paused": bot_paused}
    return {"history": [], "bot_paused": False}


async def db_toggle_bot(phone: str, bot_number: str, pause: bool):
    """
    Actualiza el estado de bot_paused para una conversación dada por phone.
    Crea el registro si no existe.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, bot_number, bot_paused, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (phone, bot_number)
            DO UPDATE SET bot_paused=EXCLUDED.bot_paused, updated_at=NOW()
        """, phone, bot_number, pause)
        # ─────────────────────────────────────────────
