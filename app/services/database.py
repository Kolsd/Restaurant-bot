import os
import asyncpg
import json
from datetime import date, datetime

_pool = None


def _normalize_phone(number: str) -> str:
    if not number:
        return ""
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
            database_url, min_size=1, max_size=5,
            init=lambda conn: conn.set_type_codec(
                'jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog'
            )
        )
    return _pool


async def init_db():
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
                created_at TIMESTAMP DEFAULT NOW()
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
                created_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS carts (
                phone TEXT NOT NULL,
                bot_number TEXT NOT NULL,
                cart_data JSONB NOT NULL DEFAULT '{"items": [], "order_type": null, "address": null, "notes": ""}'::jsonb,
                updated_at TIMESTAMP DEFAULT NOW(),
                PRIMARY KEY (phone, bot_number)
            );
        """)
        # Safe migrations
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
        ]
        for m in migrations:
            try:
                await conn.execute(m)
            except Exception as e:
                print(f"Migration skip: {e}")

        # Fix primary key on conversations if needed
        try:
            await conn.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_pkey")
            await conn.execute("ALTER TABLE conversations ADD CONSTRAINT conversations_pkey PRIMARY KEY (phone, bot_number)")
        except Exception:
            pass

        # Demo user
        import hashlib
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name)
                VALUES ($1,$2,$3) ON CONFLICT (username) DO NOTHING
            """, "demo@restaurante.com", hashlib.sha256("demo123".encode()).hexdigest(), "Demo Restaurante")
        except Exception:
            pass

    print("Base de datos inicializada")


# ── RESERVACIONES ──────────────────────────────────────────

async def db_add_reservation(name, date_str, time, guests, phone, bot_number: str = "", notes=""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO reservations (name, date, time, guests, phone, bot_number, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
        """, name, date_str, time, int(guests), phone, bot_number, notes)
        return _serialize(dict(row))


async def db_get_reservations_today(date_str: str, bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM reservations WHERE date=$1 AND bot_number=$2 ORDER BY time", date_str, bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM reservations WHERE date=$1 ORDER BY time", date_str)
        return [_serialize(dict(r)) for r in rows]


async def db_get_reservations_range(date_from: str, date_to: str, bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("""
                SELECT * FROM reservations
                WHERE date >= $1 AND date <= $2 AND bot_number=$3
                ORDER BY date, time
            """, date_from, date_to, bot_number)
        else:
            rows = await conn.fetch("""
                SELECT * FROM reservations WHERE date >= $1 AND date <= $2 ORDER BY date, time
            """, date_from, date_to)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_reservations(bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("SELECT * FROM reservations WHERE bot_number=$1 ORDER BY created_at DESC", bot_number)
        else:
            rows = await conn.fetch("SELECT * FROM reservations ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]


# ── ORDENES ──────────────────────────────────────────

async def db_save_order(order: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO orders (id, phone, items, order_type, address, notes,
                subtotal, delivery_fee, total, status, paid, payment_url, bot_number)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (id) DO UPDATE SET
                status=EXCLUDED.status, paid=EXCLUDED.paid, payment_url=EXCLUDED.payment_url
        """,
        order["id"], order["phone"], json.dumps(order["items"]),
        order["order_type"], order.get("address", ""), order.get("notes", ""),
        order["subtotal"], order["delivery_fee"], order["total"],
        order["status"], order["paid"], order.get("payment_url", ""),
        order.get("bot_number", ""))


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
    from datetime import timedelta
    d_from = _to_date(date_from)
    d_to_inclusive = _to_date(date_to) + timedelta(days=1)
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("""
                SELECT * FROM orders
                WHERE created_at >= $1 AND created_at < $2 AND bot_number=$3
                ORDER BY created_at DESC
            """, d_from, d_to_inclusive, bot_number)
        else:
            rows = await conn.fetch("""
                SELECT * FROM orders WHERE created_at >= $1 AND created_at < $2 ORDER BY created_at DESC
            """, d_from, d_to_inclusive)
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


# ── CONVERSACIONES ──────────────────────────────────────────

async def db_get_history(phone: str, bot_number: str = "") -> list:
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


async def db_get_all_conversations(bot_number: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if bot_number:
            rows = await conn.fetch("""
                SELECT phone, bot_number, history, updated_at FROM conversations
                WHERE bot_number=$1 OR bot_number=''
                ORDER BY updated_at DESC
            """, bot_number)
        else:
            rows = await conn.fetch(
                "SELECT phone, bot_number, history, updated_at FROM conversations ORDER BY updated_at DESC")
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


async def db_get_conversation_details(phone: str, bot_number: str = ""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history, bot_paused FROM conversations WHERE phone=$1 AND bot_number=$2", phone, bot_number)
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
            await conn.execute("""
                DELETE FROM conversations
                WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL AND bot_number=$2
            """, str(days), bot_number)
        else:
            await conn.execute("""
                DELETE FROM conversations WHERE updated_at < NOW() - ($1 || ' days')::INTERVAL
            """, str(days))


# ── USUARIOS ──────────────────────────────────────────

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


# ── RESTAURANTES ──────────────────────────────────────────

async def db_get_restaurant_by_phone(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(whatsapp_number.strip()))
        return _serialize(dict(row)) if row else None


async def db_get_restaurant_by_bot_number(whatsapp_number: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE whatsapp_number=$1",
            _normalize_phone(whatsapp_number))
        return _serialize(dict(row)) if row else None


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


async def db_create_restaurant(name: str, whatsapp_number: str, address: str, menu: dict,
                                latitude: float = None, longitude: float = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO restaurants (name, whatsapp_number, address, menu, latitude, longitude)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (whatsapp_number) DO UPDATE
            SET name=EXCLUDED.name, address=EXCLUDED.address, menu=EXCLUDED.menu,
                latitude=EXCLUDED.latitude, longitude=EXCLUDED.longitude
        """, name, whatsapp_number, address, json.dumps(menu), latitude, longitude)


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
        await conn.execute(
            "UPDATE restaurants SET subscription_status=$2 WHERE id=$1", restaurant_id, new_status)


# ── MENU AVAILABILITY ──────────────────────────────────────────

async def db_get_menu_availability():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS menu_availability (
                dish_name TEXT PRIMARY KEY,
                available BOOLEAN DEFAULT TRUE,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
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


# ── MESAS ──────────────────────────────────────────

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
        try:
            await conn.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER")
        except Exception:
            pass


async def db_get_tables(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await db_init_tables()
        if branch_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number", branch_id)
        else:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")
        return [_serialize(dict(r)) for r in rows]


async def db_create_table(table_id: str, number: int, name: str, branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await db_init_tables()
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id, active)
            VALUES ($1,$2,$3,$4,TRUE)
            ON CONFLICT (id) DO UPDATE SET
                number=EXCLUDED.number, name=EXCLUDED.name,
                branch_id=EXCLUDED.branch_id, active=TRUE
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
            INSERT INTO table_orders (id, table_id, table_name, phone, items, status, notes, total)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items, status=EXCLUDED.status,
                notes=EXCLUDED.notes, total=EXCLUDED.total, updated_at=NOW()
        """, order['id'], order['table_id'], order['table_name'], order['phone'],
            json.dumps(order['items']), order.get('status', 'recibido'),
            order.get('notes', ''), order.get('total', 0))


async def db_get_table_orders(status: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch("SELECT * FROM table_orders WHERE status=$1 ORDER BY created_at DESC", status)
        else:
            rows = await conn.fetch("""
                SELECT * FROM table_orders WHERE status NOT IN ('entregado','cancelado') ORDER BY created_at ASC
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
        await conn.execute("UPDATE table_orders SET status=$2, updated_at=NOW() WHERE id=$1", order_id, status)
        # ── SESIONES Y CARRITOS (RAM Fix) ──────────────────────────────────────────

async def db_save_session(token: str, username: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO sessions (token, username) VALUES ($1, $2)", token, username)

async def db_get_session(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT username FROM sessions WHERE token=$1", token)
        return row["username"] if row else None

async def db_delete_session(token: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM sessions WHERE token=$1", token)

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