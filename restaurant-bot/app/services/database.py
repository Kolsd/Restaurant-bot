import os
import asyncpg
import json

_pool = None


def _serialize(d: dict) -> dict:
    """Convierte tipos de asyncpg a tipos Python serializables."""
    result = {}
    for k, v in d.items():
        if hasattr(v, 'isoformat'):
            result[k] = v.isoformat()[:19]
        elif isinstance(v, str):
            # JSONB puede venir como string o como objeto según el driver
            if k in ('items', 'history') and v.startswith('['):
                try:
                    result[k] = json.loads(v)
                except Exception:
                    result[k] = v
            else:
                result[k] = v
        elif v is None:
            result[k] = None
        else:
            result[k] = v
    # items/history ya puede venir como list directo de asyncpg JSONB
    if 'items' in result and isinstance(result['items'], list):
        pass  # ya está bien
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
            # Serializa JSONB como string para control manual
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
        """)
        await conn.execute("""
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
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT PRIMARY KEY,
                history JSONB NOT NULL DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("""
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
            VALUES ($1, $2, $3) ON CONFLICT (username) DO NOTHING;
        """, "demo@restaurante.com",
            hashlib.sha256("demo123".encode()).hexdigest(),
            "La Trattoria Italiana")
    print("✅ Base de datos inicializada")


# ─────────────────────────────────────────────
# RESERVACIONES
# ─────────────────────────────────────────────

async def db_add_reservation(name, date, time, guests, phone, notes=""):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO reservations (name, date, time, guests, phone, notes)
            VALUES ($1,$2,$3,$4,$5,$6) RETURNING *
        """, name, date, time, int(guests), phone, notes)
        return _serialize(dict(row))


async def db_get_reservations_today(date: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reservations WHERE date=$1 ORDER BY time", date)
        return [_serialize(dict(r)) for r in rows]


async def db_get_all_reservations():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM reservations ORDER BY created_at DESC")
        return [_serialize(dict(r)) for r in rows]


async def db_get_reservations_range(date_from: str, date_to: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM reservations
            WHERE date BETWEEN $1 AND $2 ORDER BY date, time
        """, date_from, date_to)
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
        if row:
            return _serialize(dict(row))
        return None


async def db_get_orders_today(date: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM orders WHERE DATE(created_at)=$1::date
            ORDER BY created_at DESC
        """, date)
        return [_serialize(dict(r)) for r in rows]


async def db_get_orders_range(date_from: str, date_to: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM orders
            WHERE DATE(created_at) BETWEEN $1::date AND $2::date
            ORDER BY created_at DESC
        """, date_from, date_to)
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
