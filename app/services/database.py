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
            "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS billing_config JSONB DEFAULT NULL"
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT NOW()",
        ]
        
        for m in migrations:
            try:
                await conn.execute(m)
            except Exception as e:
                pass
        try:
            await conn.execute("ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_pkey")
            await conn.execute("ALTER TABLE conversations ADD CONSTRAINT conversations_pkey PRIMARY KEY (phone, bot_number)")
        except Exception:
            pass
        import hashlib
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name)
                VALUES ($1,$2,$3) ON CONFLICT (username) DO NOTHING
            """, "demo@restaurante.com", hashlib.sha256("demo123".encode()).hexdigest(), "Demo Restaurante")
        except Exception:
            pass
    print("Base de datos inicializada")


# ── RESERVACIONES ────────────────────────────────────────────────────
async def db_add_reservation(name, date_str, time, guests, phone, bot_number: str = "", notes=""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO reservations (name, date, time, guests, phone, bot_number, notes)
            VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
        """, name, date_str, time, int(guests), phone, bot_number, notes)
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
            "SELECT * FROM orders WHERE order_type='delivery' AND status = ANY($1) ORDER BY created_at ASC", 
            status_list
        )
        return [_serialize(dict(r)) for r in rows]

async def db_update_order_status(order_id: str, new_status: str):
    """Actualiza el estado de un pedido (ej. listo -> en_camino -> entregado)"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status=$2 WHERE id=$1", order_id, new_status)

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
        ]:
            try: await conn.execute(col_sql)
            except Exception: pass
        try: await conn.execute("CREATE INDEX IF NOT EXISTS idx_table_orders_base ON table_orders(base_order_id)")
        except Exception: pass

async def db_get_tables(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await db_init_tables()
        if branch_id is not None:
            rows = await conn.fetch("SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number", branch_id)
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
            INSERT INTO table_orders (id, table_id, table_name, phone, items, status, notes, total, base_order_id, sub_number)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (id) DO UPDATE SET
                items=EXCLUDED.items, status=EXCLUDED.status,
                notes=EXCLUDED.notes, total=EXCLUDED.total, updated_at=NOW()
        """, order['id'], order['table_id'], order['table_name'], order['phone'],
            json.dumps(order['items']),
            order.get('status', 'recibido'), order.get('notes', ''), order.get('total', 0),
            order.get('base_order_id'),
            order.get('sub_number', 1))

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

async def db_get_base_order_id(phone: str, table_id: str) -> str | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT COALESCE(base_order_id, id) as base_id
            FROM table_orders
            WHERE phone=$1 AND table_id=$2 AND status NOT IN ('factura_entregada', 'cancelado')
            ORDER BY created_at ASC LIMIT 1
        """, phone, table_id)
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM table_sessions WHERE bot_number=$1 AND status='closed' AND closed_at > NOW() - ($2 || ' hours')::INTERVAL ORDER BY closed_at DESC", bot_number, str(hours))
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