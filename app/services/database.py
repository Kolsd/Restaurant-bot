import os
import asyncpg
import json
from datetime import date, datetime

_pool = None


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
        await conn.execute("""
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
                phone TEXT PRIMARY KEY,
                history JSONB NOT NULL DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                restaurant_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        import hashlib
        await conn.execute("""
            INSERT INTO users (username, password_hash, restaurant_name)
            VALUES ($1,$2,$3) ON CONFLICT (username) DO NOTHING;
        """, "demo@restaurante.com",
            hashlib.sha256("demo123".encode()).hexdigest(),
            "La Trattoria Italiana")
    print("✅ Base de datos inicializada")


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

async def db_get_history(phone: str) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history FROM conversations WHERE phone=$1", phone)
        if row:
            h = row["history"]
            return h if isinstance(h, list) else json.loads(h)
        return []


async def db_save_history(phone: str, history: list):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO conversations (phone, history, updated_at)
            VALUES ($1,$2,NOW())
            ON CONFLICT (phone) DO UPDATE SET history=EXCLUDED.history, updated_at=NOW()
        """, phone, json.dumps(history[-20:]))


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


async def db_create_user(username: str, password_hash: str, restaurant_name: str,
                         role: str = "owner", branch_id: int = None, parent_user: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure columns exist
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'owner';")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id INTEGER;")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_user TEXT;")
        except Exception:
            pass
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
                branch_id INTEGER,
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
            await conn.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER;")
        except Exception:
            pass


async def db_get_tables(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await db_init_tables()
        if branch_id is not None:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number",
                branch_id)
        else:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")
        return [_serialize(dict(r)) for r in rows]


async def db_create_table(table_id: str, number: int, name: str, branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure branch_id column exists
        await conn.execute("""
            ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER;
        """)
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name, branch_id=EXCLUDED.branch_id
        """, table_id, number, name, branch_id)


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


# ─────────────────────────────────────────────
# RESTAURANTS — funciones adicionales
# ─────────────────────────────────────────────

async def db_get_restaurant_by_id(restaurant_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM restaurants WHERE id=$1", restaurant_id)
        return _serialize(dict(row)) if row else None


async def db_get_all_restaurants():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM restaurants ORDER BY id")
        return [_serialize(dict(r)) for r in rows]


async def db_update_subscription(restaurant_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET subscription_status=$2 WHERE id=$1",
            restaurant_id, status)


# ─────────────────────────────────────────────
# MESAS — funciones con branch_id
# ─────────────────────────────────────────────

async def db_get_tables_by_branch(branch_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure branch_id column exists
        await conn.execute("""
            ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER;
        """)
        rows = await conn.fetch(
            "SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number",
            branch_id)
        return [_serialize(dict(r)) for r in rows]


async def db_get_table_orders_by_branch(branch_id: int, status: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Ensure branch_id column exists on table_orders
        await conn.execute("""
            ALTER TABLE table_orders ADD COLUMN IF NOT EXISTS branch_id INTEGER;
        """)
        if status:
            rows = await conn.fetch("""
                SELECT tord.* FROM table_orders tord
                JOIN restaurant_tables rt ON rt.id = tord.table_id
                WHERE rt.branch_id=$1 AND tord.status=$2
                ORDER BY tord.created_at DESC
            """, branch_id, status)
        else:
            rows = await conn.fetch("""
                SELECT tord2.* FROM table_orders tord2
                JOIN restaurant_tables rt ON rt.id = tord2.table_id
                WHERE rt.branch_id=$1 AND tord2.status NOT IN ('entregado','cancelado')
                ORDER BY tord2.created_at ASC
            """, branch_id)
        result = []
        for r in rows:
            d = _serialize(dict(r))
            if isinstance(d.get('items'), str):
                d['items'] = json.loads(d['items'])
            result.append(d)
        return result


# ─────────────────────────────────────────────
# FUNCIONES FALTANTES — roles, branches, tables
# ─────────────────────────────────────────────

async def db_get_all_restaurants():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, whatsapp_number, address, subscription_status FROM restaurants ORDER BY id")
        return [_serialize(dict(r)) for r in rows]


async def db_get_restaurant_by_id(restaurant_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM restaurants WHERE id=$1", restaurant_id)
        return _serialize(dict(row)) if row else None


async def db_update_subscription(restaurant_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE restaurants SET subscription_status=$2 WHERE id=$1",
            restaurant_id, status)


async def db_get_tables(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Add branch_id column if not exists
        try:
            await conn.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER;")
        except Exception:
            pass
        if branch_id:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE active=TRUE AND branch_id=$1 ORDER BY number",
                branch_id)
        else:
            rows = await conn.fetch(
                "SELECT * FROM restaurant_tables WHERE active=TRUE ORDER BY number")
        return [_serialize(dict(r)) for r in rows]


async def db_create_table_v2(table_id: str, number: int, name: str, branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id INTEGER;")
        except Exception:
            pass
        await conn.execute("""
            INSERT INTO restaurant_tables (id, number, name, branch_id)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET number=EXCLUDED.number, name=EXCLUDED.name, branch_id=EXCLUDED.branch_id
        """, table_id, number, name, branch_id)


async def db_create_user_v2(username: str, password_hash: str, restaurant_name: str,
                             role: str = "owner", branch_id: int = None, parent_user: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Add columns if not exist
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'owner';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id INTEGER;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS parent_user TEXT;",
        ]:
            try:
                await conn.execute(col_sql)
            except Exception:
                pass
        try:
            await conn.execute("""
                INSERT INTO users (username, password_hash, restaurant_name, role, branch_id, parent_user)
                VALUES ($1,$2,$3,$4,$5,$6)
            """, username.lower().strip(), password_hash, restaurant_name, role, branch_id, parent_user)
            return True
        except Exception:
            return False


async def db_get_all_users_with_roles(branch_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        for col_sql in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'owner';",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS branch_id INTEGER;",
        ]:
            try:
                await conn.execute(col_sql)
            except Exception:
                pass
        if branch_id:
            rows = await conn.fetch(
                "SELECT username, restaurant_name, role, branch_id FROM users WHERE branch_id=$1",
                branch_id)
        else:
            rows = await conn.fetch(
                "SELECT username, restaurant_name, role, branch_id FROM users ORDER BY created_at")
        return [dict(r) for r in rows]
