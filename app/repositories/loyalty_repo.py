"""
app/repositories/loyalty_repo.py

Repository for loyalty/fidelización functions.
Extracted from database.py — Fase 6 Repository Pattern.
"""
import json
from app.services.logging import get_logger

log = get_logger(__name__)


def _get_pool():
    from app.services.database import get_pool
    return get_pool()


def _serialize(d: dict) -> dict:
    from app.services.database import _serialize
    return _serialize(d)


def _normalize_phone(number: str) -> str:
    if not number: return ""
    return number.replace(" ", "").replace("+", "")


async def _ensure_loyalty_tables() -> None:
    """No-op: loyalty_customers and loyalty_ledger managed by Alembic (0020_missing_runtime_tables.py)."""
    pass


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
    pool = await _get_pool()
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
    pool = await _get_pool()
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
    pool = await _get_pool()
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
    pool = await _get_pool()
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
    pool = await _get_pool()
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
    pool = await _get_pool()
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
    pool = await _get_pool()
    async with pool.acquire() as conn:
        phone = await conn.fetchval(
            "SELECT phone FROM table_orders WHERE id=$1 OR base_order_id=$1 LIMIT 1",
            base_order_id,
        )
    return phone
